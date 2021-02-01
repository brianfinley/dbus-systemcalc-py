import gobject
import logging
import os
import traceback
from glob import glob

# Victron packages
from ve_utils import exit_on_error

from delegates.base import SystemCalcDelegate

class RelayState(SystemCalcDelegate):
	RELAY_GLOB = '/dev/gpio/relay_*'
	FUNCTION_ALARM = 0
	FUNCTION_MANUAL = 2
	FUNCTION_BMS_STOPCHARGE = 4
	FUNCTION_BMS_STOPDISCHARGE = 5

	def __init__(self):
		SystemCalcDelegate.__init__(self)
		self._relays = {}

	def get_input(self):
		return [
			('com.victronenergy.settings', [
				 '/Settings/Relay/Function',
				 '/Settings/Relay/1/Function'])] # Managed by the gui

	def get_settings(self):
		return [
			('/Relay/0/State', '/Settings/Relay/0/InitialState', 0, 0, 1),
			('/Relay/1/State', '/Settings/Relay/1/InitialState', 0, 0, 1)
		]

	@property
	def relay_function(self):
		return self._dbusmonitor.get_value('com.victronenergy.settings',
			'/Settings/Relay/Function')

	@property
	def relay1_function(self):
		return self._dbusmonitor.get_value('com.victronenergy.settings',
			'/Settings/Relay/1/Function')

	def set_sources(self, dbusmonitor, settings, dbusservice):
		SystemCalcDelegate.set_sources(self, dbusmonitor, settings, dbusservice)
		relays = sorted(glob(self.RELAY_GLOB))

		if len(relays) == 0:
			logging.info('No relays found')
			return

		self._relays.update({'/Relay/{}/State'.format(i): os.path.join(r, 'value') \
			for i, r in enumerate(relays) })

		gobject.idle_add(exit_on_error, self._init_relay_state)
		for dbus_path in self._relays.iterkeys():
			self._dbusservice.add_path(dbus_path, value=None, writeable=True,
				onchangecallback=self._on_relay_state_changed)

		logging.info('Relays found: {}'.format(', '.join(self._relays.values())))

	def set_relay(self, dbus_path, state):
		self._dbusservice[dbus_path] = state
		self.__on_relay_state_changed(dbus_path, state)

	def _init_relay_state(self):
		if self.relay_function is None:
			return True # Try again on the next idle event

		for dbus_path, path in self._relays.iteritems():
			if dbus_path == '/Relay/0/State' and self.relay_function != self.FUNCTION_MANUAL:
				continue # Skip primary relay if function is not manual
			if dbus_path == '/Relay/1/State' and self.relay1_function != self.FUNCTION_MANUAL:
				continue # Skip secondary relay if function is not manual

			state = self._settings[dbus_path]
			self.set_relay(dbus_path, state)

		# Sync state back to dbus
		self._update_relay_state()

		# Watch changes and update dbus. Do we still need this?
		gobject.timeout_add(5000, exit_on_error, self._update_relay_state)
		return False

	def _update_relay_state(self):
		# @todo EV Do we still need this? Maybe only at startup?
		for dbus_path, file_path in self._relays.items():
			try:
				with open(file_path, 'rt') as r:
					state = int(r.read().strip())
					self._dbusservice[dbus_path] = state
			except (IOError, ValueError):
				traceback.print_exc()
		return True

	def __on_relay_state_changed(self, dbus_path, state):
		try:
			path = self._relays[dbus_path]
			with open(path, 'wt') as w:
				w.write(str(state))
		except IOError:
			traceback.print_exc()
			return False
		return True

	def _on_relay_state_changed(self, dbus_path, value):
		try:
			state = int(bool(value))
		except ValueError:
			traceback.print_exc()
			return False
		try:
			return self.__on_relay_state_changed(dbus_path, state)
		finally:
			# Remember the state to restore after a restart
			self._settings[dbus_path] = state
