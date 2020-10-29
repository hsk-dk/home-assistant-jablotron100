import binascii
from concurrent.futures import ThreadPoolExecutor
import datetime
from homeassistant import core
from homeassistant.const import (
	CONF_PASSWORD,
	EVENT_HOMEASSISTANT_STOP,
	STATE_ALARM_DISARMED,
	STATE_ALARM_ARMED_AWAY,
	STATE_ALARM_ARMED_NIGHT,
	STATE_ALARM_ARMING,
	STATE_ALARM_PENDING,
	STATE_ALARM_TRIGGERED,
	STATE_OFF,
	STATE_ON,
)
from homeassistant.helpers import storage
from homeassistant.helpers.entity import Entity
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from .const import (
	CONF_DEVICES,
	CONF_NUMBER_OF_DEVICES,
	CONF_SERIAL_PORT,
	CONF_REQUIRE_CODE_TO_ARM,
	CONF_REQUIRE_CODE_TO_DISARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_ARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_DISARM,
	DEVICES,
	DEVICE_EMPTY,
	DEVICE_KEYPAD,
	DEVICE_SIREN_OUTDOOR,
	DEVICE_OTHER,
	DOMAIN,
	LOGGER,
	MAX_SECTIONS,
)
from .errors import (
	ModelNotDetected,
	ModelNotSupported,
	ServiceUnavailable,
	ShouldNotHappen,
)

MAX_WORKERS = 5
TIMEOUT = 10
PACKET_READ_SIZE = 64

STORAGE_VERSION = 1
STORAGE_STATES_KEY = "states"

# x02 model
# x08 hardware version
# x09 firmware version
# x0a registration code
# x0b name of the installation
JABLOTRON_PACKET_GET_MODEL = b"\x30\x01\x02"
JABLOTRON_PACKET_GET_INFO = b"\x30\x01\x02\x30\x01\x08\x30\x01\x09"
JABLOTRON_PACKET_GET_SECTIONS_STATES = b"\x80\x01\x01\x52\x01\x0e"
JABLOTRON_PACKET_SECTIONS_STATES_PREFIX = b"\x51\x22"
JABLOTRON_PACKET_DEVICES_STATES_PREFIX = b"\xd8"
JABLOTRON_PACKET_WIRED_DEVICE_STATE_PREFIX = b"\x55\x08"
JABLOTRON_PACKET_WIRELESS_DEVICE_STATE_PREFIX = b"\x55\x09"
JABLOTRON_PACKET_INFO_PREFIX = b"\x40"
JABLOTRON_PACKETS_DEVICE_ACTIVITY = [b"\x00", b"\x01", b"\x0a", b"\x0c", b"\x24", b"\x3e", b"\x80", b"\x81", b"\xa3", b"\xa4", b"\xa6", b"\xbe"]
JABLOTRON_INFO_MODEL = b"\x02"
JABLOTRON_INFO_HARDWARE_VERSION = b"\x08"
JABLOTRON_INFO_FIRMWARE_VERSION = b"\x09"
JABLOTRON_INFO_REGISTRATION_CODE = b"\x0a"
JABLOTRON_INFO_INSTALLATION_NAME = b"\x0b"

JABLOTRON_SECTION_PRIMARY_STATE_DISARMED = 1
JABLOTRON_SECTION_PRIMARY_STATE_ARMED_PARTIALLY = 2
JABLOTRON_SECTION_PRIMARY_STATE_ARMED_FULL = 3
JABLOTRON_SECTION_PRIMARY_STATE_TRIGGERED = 11
JABLOTRON_SECTION_PRIMARY_STATES = [
	JABLOTRON_SECTION_PRIMARY_STATE_DISARMED,
	JABLOTRON_SECTION_PRIMARY_STATE_ARMED_PARTIALLY,
	JABLOTRON_SECTION_PRIMARY_STATE_ARMED_FULL,
	JABLOTRON_SECTION_PRIMARY_STATE_TRIGGERED,
]

JABLOTRON_SECTION_SECONDARY_STATE_OK = 0
JABLOTRON_SECTION_SECONDARY_STATE_TRIGGERED = 1
JABLOTRON_SECTION_SECONDARY_STATE_PROBLEM = 2
JABLOTRON_SECTION_SECONDARY_STATE_PENDING = 4
JABLOTRON_SECTION_SECONDARY_STATE_ARMING = 8
JABLOTRON_SECTION_SECONDARY_STATES = [
	JABLOTRON_SECTION_SECONDARY_STATE_OK,
	JABLOTRON_SECTION_SECONDARY_STATE_TRIGGERED,
	JABLOTRON_SECTION_SECONDARY_STATE_PROBLEM,
	JABLOTRON_SECTION_SECONDARY_STATE_PENDING,
	JABLOTRON_SECTION_SECONDARY_STATE_ARMING,
]

JABLOTRON_SECTION_TERTIARY_STATE_OFF = 0
JABLOTRON_SECTION_TERTIARY_STATE_ON = 1
JABLOTRON_SECTION_TERTIARY_STATE_TRIGGERED = 17
JABLOTRON_SECTION_TERTIARY_STATES = [
	JABLOTRON_SECTION_TERTIARY_STATE_OFF,
	JABLOTRON_SECTION_TERTIARY_STATE_ON,
	JABLOTRON_SECTION_TERTIARY_STATE_TRIGGERED,
]


def decode_info_bytes(value: bytes) -> str:
	info = ""

	for i in range(len(value)):
		letter = value[i:(i + 1)]

		if letter == b"\x00" or letter == JABLOTRON_PACKET_INFO_PREFIX:
			break

		info += letter.decode()

	return info


def check_serial_port(serial_port: str) -> None:
	stop_event = threading.Event()
	thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

	def reader_thread() -> Optional[str]:
		detected_model = None

		stream = open(serial_port, "rb")

		try:
			while not stop_event.is_set():
				packet = stream.read(PACKET_READ_SIZE)
				LOGGER.debug("Info packet: {}".format(Jablotron.format_packet_to_string(packet)))

				if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX and packet[2:3] == JABLOTRON_INFO_MODEL:
					try:
						detected_model = decode_info_bytes(packet[3:])
						break
					except UnicodeDecodeError:
						# Try again
						pass
		finally:
			stream.close()

		return detected_model

	def writer_thread() -> None:
		while not stop_event.is_set():
			stream = open(serial_port, "wb")

			stream.write(JABLOTRON_PACKET_GET_MODEL)
			time.sleep(0.1)

			stream.close()

			time.sleep(1)

	try:
		reader = thread_pool_executor.submit(reader_thread)
		thread_pool_executor.submit(writer_thread)

		model = reader.result(TIMEOUT)

		if model is None:
			raise ModelNotDetected

		if not re.match(r"^JA-10[1367]", model):
			LOGGER.debug("Unsupported model: {}", model)
			raise ModelNotSupported("Model {} not supported".format(model))

	except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError):
		raise ServiceUnavailable

	finally:
		stop_event.set()
		thread_pool_executor.shutdown()


class JablotronCentralUnit:

	def __init__(self, serial_port: str, model: str, hardware_version: str, firmware_version: str):
		self.serial_port: str = serial_port
		self.model: str = model
		self.hardware_version: str = hardware_version
		self.firmware_version: str = firmware_version


class JablotronHassDevice:

	def __init__(self, id: str, name: str):
		self.id: str = id
		self.name: str = name


class JablotronControl:

	def __init__(self, central_unit: JablotronCentralUnit, hass_device: Optional[JablotronHassDevice], id: str, name: str):
		self.central_unit: JablotronCentralUnit = central_unit
		self.hass_device: Optional[JablotronHassDevice] = hass_device
		self.id: str = id
		self.name: str = name


class JablotronDevice(JablotronControl):

	def __init__(self, central_unit: JablotronCentralUnit, hass_device: JablotronHassDevice, id: str, name: str, type: str):
		self.type: str = type

		super().__init__(central_unit, hass_device, id, name)


class JablotronAlarmControlPanel(JablotronControl):

	def __init__(self, central_unit: JablotronCentralUnit, hass_device: JablotronHassDevice, id: str, name: str, section: int):
		self.section: int = section

		super().__init__(central_unit, hass_device, id, name)


class Jablotron:

	def __init__(self, hass: core.HomeAssistant, config: Dict[str, Any], options: Dict[str, Any]) -> None:
		self._hass: core.HomeAssistant = hass
		self._config: Dict[str, Any] = config
		self._options: Dict[str, Any] = options

		self._central_unit: Optional[JablotronCentralUnit] = None
		self._alarm_control_panels: List[JablotronAlarmControlPanel] = []
		self._section_problem_sensors: List[JablotronControl] = []
		self._device_sensors: List[JablotronDevice] = []
		self._device_problem_sensors: List[JablotronControl] = []
		self._lan_connection: Optional[JablotronControl] = None

		self._entities: Dict[str, JablotronEntity] = {}

		self._state_checker_thread_pool_executor: Optional[ThreadPoolExecutor] = None
		self._state_checker_stop_event: threading.Event = threading.Event()
		self._state_checker_data_updating_event: threading.Event = threading.Event()

		self._store: storage.Store = storage.Store(hass, STORAGE_VERSION, DOMAIN)
		self._stored_data: Optional[dict] = None

		self.states: Dict[str, str] = {}
		self.last_update_success: bool = False

	def update_options(self, options: Dict[str, Any]) -> None:
		self._options = options
		self._update_all_entities()

	def is_code_required_for_disarm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_DISARM, DEFAULT_CONF_REQUIRE_CODE_TO_DISARM)

	def is_code_required_for_arm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_ARM, DEFAULT_CONF_REQUIRE_CODE_TO_ARM)

	async def initialize(self) -> None:
		def shutdown_event(_):
			self.shutdown()

		self._hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, shutdown_event)

		await self._load_stored_data()

		self._detect_central_unit()
		self._detect_sections()
		self._create_devices()
		self._create_lan_connection()

		# Initialize states checker
		self._state_checker_thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
		self._state_checker_thread_pool_executor.submit(self._read_packets)
		self._state_checker_thread_pool_executor.submit(self._keepalive)

		self.last_update_success = True

	def central_unit(self) -> JablotronCentralUnit:
		return self._central_unit

	def shutdown(self) -> None:
		self._state_checker_stop_event.set()

		# Send packet so read thread can finish
		self._send_packet(JABLOTRON_PACKET_GET_SECTIONS_STATES)

		if self._state_checker_thread_pool_executor is not None:
			self._state_checker_thread_pool_executor.shutdown()

	def substribe_entity_for_updates(self, control_id: str, entity) -> None:
		self._entities[control_id] = entity

	def modify_alarm_control_panel_section_state(self, section: int, state: str, code: Optional[str]) -> None:
		if code is None:
			code = self._config[CONF_PASSWORD]

		int_packets = {
			STATE_ALARM_DISARMED: 143,
			STATE_ALARM_ARMED_AWAY: 159,
			STATE_ALARM_ARMED_NIGHT: 175,
		}

		state_packet = Jablotron._int_to_bytes(int_packets[state] + section)

		self._send_packet(Jablotron._create_code_packet(code) + b"\x80\x02\x0d" + state_packet)

	def alarm_control_panels(self) -> List[JablotronAlarmControlPanel]:
		return self._alarm_control_panels

	def section_problem_sensors(self) -> List[JablotronControl]:
		return self._section_problem_sensors

	def device_sensors(self) -> List[JablotronDevice]:
		return self._device_sensors

	def device_problem_sensors(self) -> List[JablotronControl]:
		return self._device_problem_sensors

	def lan_connection(self) -> Optional[JablotronControl]:
		return self._lan_connection

	def _update_all_entities(self) -> None:
		for entity in self._entities.values():
			entity.async_write_ha_state()

	async def _load_stored_data(self) -> None:
		self._stored_data = await self._store.async_load()

		if self._stored_data is None:
			self._stored_data = {}

		serial_port = self._config[CONF_SERIAL_PORT]

		if serial_port not in self._stored_data:
			return

		if STORAGE_STATES_KEY not in self._stored_data[serial_port]:
			return

		self.states = self._stored_data[serial_port][STORAGE_STATES_KEY]

	def _detect_central_unit(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[JablotronCentralUnit]:
			model = None
			hardware_version = None
			firmware_version = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)

					if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX:
						LOGGER.debug("Info packet: {}".format(Jablotron.format_packet_to_string(packet)))

						info_packets = []

						for i in range(len(packet)):
							prefix = packet[i:(i + 1)]

							if prefix == JABLOTRON_PACKET_INFO_PREFIX:
								info_packets.append(packet[i:])

						for info_packet in info_packets:
							try:
								if info_packet[2:3] == JABLOTRON_INFO_MODEL:
									model = decode_info_bytes(info_packet[3:])
								elif info_packet[2:3] == JABLOTRON_INFO_HARDWARE_VERSION:
									hardware_version = decode_info_bytes(info_packet[3:])
								elif info_packet[2:3] == JABLOTRON_INFO_FIRMWARE_VERSION:
									firmware_version = decode_info_bytes(info_packet[3:])
							except UnicodeDecodeError:
								# Try again
								pass

					if model is not None and hardware_version is not None and firmware_version is not None:
						break
			finally:
				stream.close()

			if model is None or hardware_version is None or firmware_version is None:
				return None

			return JablotronCentralUnit(self._config[CONF_SERIAL_PORT], model, hardware_version, firmware_version)

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_INFO)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			self._central_unit = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if self._central_unit is None:
			raise ShouldNotHappen

	def _detect_sections(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[bytes]:
			states_packet = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)

					if packet[:2] == JABLOTRON_PACKET_SECTIONS_STATES_PREFIX:
						states_packet = packet
						break
			finally:
				stream.close()

			return states_packet

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_SECTIONS_STATES)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			sections_states_packet = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if sections_states_packet is None:
			raise ShouldNotHappen

		section_states = Jablotron._parse_sections_states_packet(sections_states_packet)

		for section, section_packet in section_states.items():
			section_hass_device = Jablotron._create_section_hass_device(section)
			section_alarm_id = Jablotron._create_section_alarm_id(section)
			section_problem_sensor_id = Jablotron._create_section_problem_sensor_id(section)

			self._alarm_control_panels.append(JablotronAlarmControlPanel(
				self._central_unit,
				section_hass_device,
				section_alarm_id,
				Jablotron._create_section_alarm_name(section),
				section,
			))
			self._section_problem_sensors.append(JablotronControl(
				self._central_unit,
				section_hass_device,
				section_problem_sensor_id,
				Jablotron._create_section_problem_sensor_name(section),
			))

			section_state = Jablotron._parse_jablotron_section_state(section_packet)

			if not Jablotron._is_known_section_state(section_state):
				LOGGER.error("Unknown state packet for section {}: {}".format(section, Jablotron.format_packet_to_string(sections_states_packet)))

			self._set_initial_state(section_alarm_id, Jablotron._convert_jablotron_section_state_to_alarm_state(section_state))
			self._set_initial_state(section_problem_sensor_id, Jablotron._convert_jablotron_section_state_to_problem_sensor_state(section_state))

	def _create_devices(self) -> None:
		for i in range(self._config[CONF_NUMBER_OF_DEVICES]):
			number = i + 1

			if self._is_device_ignored(number):
				continue

			type = self._get_device_type(number)

			hass_device = Jablotron._create_device_hass_device(type, number)
			device_sensor_id = Jablotron._create_device_sensor_id(number)
			device_problem_sensor_id = Jablotron._create_device_problem_sensor_id(number)

			if self._is_device_with_activity_sensor(number):
				self._device_sensors.append(JablotronDevice(
					self._central_unit,
					hass_device,
					device_sensor_id,
					Jablotron._create_device_sensor_name(type, number),
					type,
				))

			self._device_problem_sensors.append(JablotronControl(
				self._central_unit,
				hass_device,
				device_problem_sensor_id,
				Jablotron._create_device_problem_sensor_name(type, number),
			))

			self._set_initial_state(device_sensor_id, STATE_OFF)
			self._set_initial_state(device_problem_sensor_id, STATE_OFF)

	def _create_lan_connection(self) -> None:
		if self._get_lan_connection_device_number() is None:
			return None

		id = self._create_lan_connection_id()

		self._lan_connection = JablotronControl(
			self._central_unit,
			None,
			id,
			self._create_lan_connection_name(),
		)

		self._set_initial_state(id, STATE_ON)

	def _read_packets(self) -> None:
		stream = open(self._config[CONF_SERIAL_PORT], "rb")
		last_restarted_at_hour = datetime.datetime.now().hour

		while not self._state_checker_stop_event.is_set():

			try:

				while True:

					actual_hour = datetime.datetime.now().hour
					if last_restarted_at_hour != actual_hour:
						stream.close()
						stream = open(self._config[CONF_SERIAL_PORT], "rb")
						last_restarted_at_hour = actual_hour

					self._state_checker_data_updating_event.clear()

					packet = stream.read(PACKET_READ_SIZE)
					# LOGGER.debug(Jablotron.format_packet_to_string(packet))

					self._state_checker_data_updating_event.set()

					if not packet:
						self.last_update_success = False
						self._update_all_entities()
						break

					if self.last_update_success is False:
						self.last_update_success = True
						self._update_all_entities()

					prefix = packet[:2]

					if prefix == JABLOTRON_PACKET_SECTIONS_STATES_PREFIX:
						self._parse_section_states_packet(packet)
						break

					if Jablotron._is_device_state_packet(prefix):
						self._parse_device_state_packet(packet)

						for i in range(len(packet) - 1):
							possible_sections_states_prefix = packet[i:(i + 2)]

							if possible_sections_states_prefix == JABLOTRON_PACKET_SECTIONS_STATES_PREFIX:
								self._parse_section_states_packet(packet[i:])

						break

					if packet[:1] == JABLOTRON_PACKET_DEVICES_STATES_PREFIX:
						self._parse_devices_states_packet(packet)
						break

			except Exception as ex:
				LOGGER.error("Read error: {}".format(format(ex)))
				self.last_update_success = False
				self._update_all_entities()

			time.sleep(0.5)

		stream.close()

	def _keepalive(self):
		counter = 0
		while not self._state_checker_stop_event.is_set():
			if not self._state_checker_data_updating_event.wait(0.5):
				try:
					if counter == 0 and not self._is_alarm_active():
						self._send_packet(Jablotron._create_code_packet(self._config[CONF_PASSWORD]) + b"\x52\x02\x13\x05\x9a")
					else:
						self._send_packet(b"\x52\x01\x02")
				except Exception as ex:
					LOGGER.error("Write error: {}".format(format(ex)))

				counter += 1
			else:
				time.sleep(1)

			if counter == 60:
				counter = 0

	def _send_packet(self, packet) -> None:
		stream = open(self._config[CONF_SERIAL_PORT], "wb")

		stream.write(packet)
		time.sleep(0.1)

		stream.close()

	def _update_state(self, id: str, state: str, store_state: bool = False) -> None:
		if id in self.states and state == self.states[id]:
			return

		self.states[id] = state

		if id in self._entities:
			self._entities[id].async_write_ha_state()

		if store_state:
			self._store_state(id, state)

	def _is_alarm_active(self) -> bool:
		for alarm_control_panel in self._alarm_control_panels:
			section_alarm_id = Jablotron._create_section_alarm_id(alarm_control_panel.section)

			if (
				self.states[section_alarm_id] == STATE_ALARM_TRIGGERED
				or self.states[section_alarm_id] == STATE_ALARM_PENDING
			):
				return True

		return False

	def _get_device_type(self, number: int) -> str:
		return self._config[CONF_DEVICES][number - 1]

	def _is_device_ignored(self, number: int) -> bool:
		type = self._get_device_type(number)

		return type in [
			DEVICE_KEYPAD,
			DEVICE_OTHER,
			DEVICE_EMPTY,
		]

	def _is_device_with_activity_sensor(self, number: int) -> bool:
		type = self._get_device_type(number)

		return type not in [
			DEVICE_SIREN_OUTDOOR,
		]

	def _parse_section_states_packet(self, packet: bytes) -> None:
		section_states = Jablotron._parse_sections_states_packet(packet)

		for section, section_packet in section_states.items():
			section_state = Jablotron._parse_jablotron_section_state(section_packet)

			if not Jablotron._is_known_section_state(section_state):
				LOGGER.error("Unknown state packet for section {}: {}".format(section, Jablotron.format_packet_to_string(packet)))

			self._update_state(
				Jablotron._create_section_alarm_id(section),
				Jablotron._convert_jablotron_section_state_to_alarm_state(section_state),
			)

			if (
				section_state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_OK
				or section_state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_PROBLEM
			):
				self._update_state(
					Jablotron._create_section_problem_sensor_id(section),
					Jablotron._convert_jablotron_section_state_to_problem_sensor_state(section_state),
				)

	def _parse_device_state_packet(self, packet: bytes) -> None:
		device_number = Jablotron._parse_device_number_from_state_packet(packet)

		if device_number == 0:
			LOGGER.debug("State packet of central unit: {}".format(Jablotron.format_packet_to_string(packet)))
			return

		lan_connection_device_number = self._get_lan_connection_device_number()
		is_lan_connection_device = True if lan_connection_device_number == device_number else False

		if is_lan_connection_device is False:
			if device_number > self._config[CONF_NUMBER_OF_DEVICES]:
				LOGGER.debug("State packet of unknown device: {}".format(Jablotron.format_packet_to_string(packet)))
				return

			if self._is_device_ignored(device_number):
				LOGGER.debug("State packet of {}: {}".format(DEVICES[self._get_device_type(device_number)].lower(), Jablotron.format_packet_to_string(packet)))
				return

		device_state = Jablotron._convert_jablotron_device_state_to_state(packet, device_number)

		if device_state is None:
			LOGGER.error("Unknown state packet of device {}: {}".format(device_number, Jablotron.format_packet_to_string(packet)))
			return

		if is_lan_connection_device is True:
			self._update_state(
				Jablotron._create_lan_connection_id(),
				STATE_ON if device_state == STATE_OFF else STATE_OFF,
				store_state=True,
			)
		elif (
			self._is_device_with_activity_sensor(device_number)
			and Jablotron._is_device_state_packet_for_activity(packet)
		):
			self._update_state(
				Jablotron._create_device_sensor_id(device_number),
				device_state,
			)
		elif (
			Jablotron._is_device_state_packet_for_sabotage(packet)
			or Jablotron._is_device_state_packet_for_fault(packet)
		):
			self._update_state(
				Jablotron._create_device_problem_sensor_id(device_number),
				device_state,
				store_state=True,
			)
		else:
			LOGGER.error("Unknown state packet of device {}: {}".format(device_number, Jablotron.format_packet_to_string(packet)))

	def _parse_devices_states_packet(self, packet: bytes) -> None:
		states_start_packet = 3
		triggered_device_start_packet = states_start_packet + Jablotron._bytes_to_int(packet[1:2]) - 1

		states = Jablotron._hex_to_bin(packet[states_start_packet:triggered_device_start_packet])

		if Jablotron._is_device_state_packet(packet[triggered_device_start_packet:(triggered_device_start_packet + 2)]):
			self._parse_device_state_packet(packet[triggered_device_start_packet:])

		for i in range(1, self._config[CONF_NUMBER_OF_DEVICES] + 1):
			device_state = STATE_ON if states[i:(i + 1)] == "1" else STATE_OFF
			# Use only OFF state
			if device_state == STATE_OFF:
				self._update_state(
					Jablotron._create_device_sensor_id(i),
					device_state,
				)

	def _get_lan_connection_device_number(self) -> Optional[int]:
		if self._central_unit.model == "JA-101K-LAN":
			return 125

		return None

	def _set_initial_state(self, id: str, initial_state: str):
		if id in self.states:
			# Loaded from stored data
			return

		self.states[id] = initial_state

	def _store_state(self, id: str, state: str):
		serial_port = self._config[CONF_SERIAL_PORT]

		if serial_port not in self._stored_data:
			self._stored_data[serial_port] = {}

		if STORAGE_STATES_KEY not in self._stored_data[serial_port]:
			self._stored_data[serial_port][STORAGE_STATES_KEY] = {}

		self._stored_data[serial_port][STORAGE_STATES_KEY][id] = state
		self._store.async_delay_save(self._data_to_store)

	@core.callback
	def _data_to_store(self) -> dict:
		return self._stored_data

	@staticmethod
	def _create_code_packet(code: str) -> bytes:
		code_packet = b"\x80\x08\x03\x39\x39\x39"

		for i in range(0, 4):
			j = i + 4

			first_number = code[j:(j + 1)]
			second_number = code[i:(i + 1)]

			if first_number == "":
				code_number = 48 + int(second_number)
			else:
				code_number = int(f"{first_number}{second_number}", 16)

			code_packet += Jablotron._int_to_bytes(code_number)

		return code_packet

	@staticmethod
	def _is_device_state_packet(prefix) -> bool:
		return prefix == JABLOTRON_PACKET_WIRED_DEVICE_STATE_PREFIX or prefix == JABLOTRON_PACKET_WIRELESS_DEVICE_STATE_PREFIX

	@staticmethod
	def _is_device_state_packet_for_activity(packet: bytes) -> bool:
		return packet[2:3] in JABLOTRON_PACKETS_DEVICE_ACTIVITY

	@staticmethod
	def _is_device_state_packet_for_sabotage(packet: bytes) -> bool:
		return Jablotron._bytes_to_int(packet[2:3]) % 128 == 6

	@staticmethod
	def _is_device_state_packet_for_fault(packet: bytes) -> bool:
		return Jablotron._bytes_to_int(packet[2:3]) % 128 == 7

	@staticmethod
	def _parse_sections_states_packet(packet: bytes) -> Dict[int, bytes]:
		section_states = {}

		for section in range(1, MAX_SECTIONS + 1):
			state_offset = section * 2
			state = packet[state_offset:(state_offset + 2)]

			# Unused section
			if state == b"\x07\x00":
				break

			section_states[section] = state

		return section_states

	@staticmethod
	def _parse_device_number_from_state_packet(packet: bytes) -> int:
		return int(Jablotron._bytes_to_int(packet[4:6]) / 64)

	@staticmethod
	def _convert_jablotron_device_state_to_state(packet: bytes, device_number: int) -> Optional[str]:
		state = Jablotron._bytes_to_int(packet[3:4])

		if device_number <= 36:
			high_device_number_offset = 0
		elif device_number <= 96:
			high_device_number_offset = -64
		else:
			high_device_number_offset = -128

		device_states_offset = ((device_number + high_device_number_offset) * 4) + 104

		on_state = device_states_offset
		on_state_2 = device_states_offset + 1
		off_state = device_states_offset + 2
		off_state_2 = device_states_offset + 3

		if state == off_state or state == off_state_2:
			return STATE_OFF

		if state == on_state or state == on_state_2:
			return STATE_ON

		return None

	@staticmethod
	def _int_to_bytes(number: int) -> bytes:
		return int.to_bytes(number, 1, byteorder=sys.byteorder)

	@staticmethod
	def _bytes_to_int(packet: bytes) -> int:
		return int.from_bytes(packet, byteorder=sys.byteorder)

	@staticmethod
	def _hex_to_bin(hex):
		dec = Jablotron._bytes_to_int(hex)
		bin_dec = bin(dec)
		bin_string = bin_dec[2:]
		bin_string = bin_string.zfill(len(hex) * 8)
		return bin_string[::-1]

	@staticmethod
	def _create_section_hass_device(section: int) -> JablotronHassDevice:
		return JablotronHassDevice(
			"section_{}".format(section),
			"Section {}".format(section),
		)

	@staticmethod
	def _create_section_alarm_id(section: int) -> str:
		return "section_{}".format(section)

	@staticmethod
	def _create_section_alarm_name(section: int) -> str:
		return "Section {}".format(section)

	@staticmethod
	def _create_section_problem_sensor_id(section: int) -> str:
		return "section_problem_sensor_{}".format(section)

	@staticmethod
	def _create_section_problem_sensor_name(section: int) -> str:
		return "Problem of section {}".format(section)

	@staticmethod
	def _create_device_hass_device(device_type: str, device_number: int) -> JablotronHassDevice:
		return JablotronHassDevice(
			"device_{}".format(device_number),
			"{} (device {})".format(DEVICES[device_type], device_number),
		)

	@staticmethod
	def _create_device_sensor_id(device_number: int) -> str:
		return "device_sensor_{}".format(device_number)

	@staticmethod
	def _create_device_sensor_name(device_type: str, device_number: int) -> str:
		return "{} (device {})".format(DEVICES[device_type], device_number)

	@staticmethod
	def _create_device_problem_sensor_id(device_number: int) -> str:
		return "device_problem_sensor_{}".format(device_number)

	@staticmethod
	def _create_device_problem_sensor_name(device_type: str, device_number: int) -> str:
		return "Problem of {} (device {})".format(DEVICES[device_type].lower(), device_number)

	@staticmethod
	def _create_lan_connection_id() -> str:
		return "lan"

	@staticmethod
	def _create_lan_connection_name() -> str:
		return "LAN connection"

	@staticmethod
	def _is_known_section_state(state: Dict[str, int]) -> bool:
		return (
			state["primary"] in JABLOTRON_SECTION_PRIMARY_STATES
			and state["secondary"] in JABLOTRON_SECTION_SECONDARY_STATES
			and state["tertiary"] in JABLOTRON_SECTION_TERTIARY_STATES
		)

	@staticmethod
	def _convert_jablotron_section_state_to_alarm_state(state: Dict[str, int]) -> str:
		if (
			state["primary"] == JABLOTRON_SECTION_PRIMARY_STATE_TRIGGERED
			or state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_TRIGGERED
		):
			return STATE_ALARM_TRIGGERED

		if state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_ARMING:
			return STATE_ALARM_ARMING

		if state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_PENDING:
			return STATE_ALARM_PENDING

		if state["primary"] == JABLOTRON_SECTION_PRIMARY_STATE_ARMED_FULL:
			if state["tertiary"] == JABLOTRON_SECTION_TERTIARY_STATE_ON:
				return STATE_ALARM_TRIGGERED
			else:
				return STATE_ALARM_ARMED_AWAY

		if state["primary"] == JABLOTRON_SECTION_PRIMARY_STATE_ARMED_PARTIALLY:
			return STATE_ALARM_ARMED_NIGHT

		return STATE_ALARM_DISARMED

	@staticmethod
	def _convert_jablotron_section_state_to_problem_sensor_state(state: Dict[str, int]) -> str:
		return STATE_ON if state["secondary"] == JABLOTRON_SECTION_SECONDARY_STATE_PROBLEM else STATE_OFF

	@staticmethod
	def _parse_jablotron_section_state(packet: bytes) -> Dict[str, int]:
		first_packet = packet[0:1]

		number = Jablotron._bytes_to_int(first_packet)

		primary_state = number % 16
		secondary_state = int((number - primary_state) / 16)

		return {
			"primary": primary_state,
			"secondary": secondary_state,
			"tertiary": Jablotron._bytes_to_int(packet[1:2]),
		}

	@staticmethod
	def format_packet_to_string(packet: bytes) -> str:
		return str(binascii.hexlify(packet), "utf-8")


class JablotronEntity(Entity):
	_state: str

	def __init__(
			self,
			jablotron: Jablotron,
			control: JablotronControl,
	) -> None:
		self._jablotron: Jablotron = jablotron
		self._control: JablotronControl = control

	@property
	def should_poll(self) -> bool:
		return False

	@property
	def available(self) -> bool:
		return self._jablotron.last_update_success

	@property
	def device_info(self):
		if self._control.hass_device is None:
			return {
				"identifiers": {(DOMAIN, self._control.central_unit.serial_port)},
			}

		return {
			"identifiers": {(DOMAIN, self._control.hass_device.id)},
			"name": self._control.hass_device.name,
			"via_device": (DOMAIN, self._control.central_unit.serial_port),
		}

	@property
	def name(self) -> str:
		return self._control.name

	@property
	def unique_id(self) -> str:
		return "{}.{}.{}".format(DOMAIN, self._control.central_unit.serial_port, self._control.id)

	@property
	def state(self) -> str:
		return self._jablotron.states[self._control.id]

	async def async_added_to_hass(self) -> None:
		self._jablotron.substribe_entity_for_updates(self._control.id, self)

	def update_state(self, state: str) -> None:
		self._jablotron.states[self._control.id] = state
		self.async_write_ha_state()
