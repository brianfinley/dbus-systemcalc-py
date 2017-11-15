import logging
import gobject
from datetime import datetime, timedelta
from functools import partial

# Victron packages
from ve_utils import exit_on_error
from delegates.base import SystemCalcDelegate

# Path constants
BLPATH = "/Settings/CGwacs/BatteryLife";
STATE_PATH = BLPATH + "/State";
FLAGS_PATH = BLPATH + "/Flags";
SOC_LIMIT_PATH = BLPATH + "/SocLimit";
MIN_SOC_LIMIT_PATH = BLPATH + "/MinimumSocLimit";
DISCHARGED_TIME_PATH = BLPATH + "/DischargedTime";
DISCHARGED_SOC_PATH = BLPATH + "/DischargedSoc"

class State(object):
	BLDisabled = 0
	BLRestart = 1
	BLDefault = 2
	BLAbsorption = 3
	BLFloat = 4
	BLDischarged = 5
	BLForceCharge = 6
	BLSustain = 7
	# Not used any more, but we keep it, because the /BatteryLife/State setting
	# this value.
	BLLowSocCharge = 8
	KeepCharged = 9
	SocGuardDefault = 10
	SocGuardDischarged = 11
	# Not used any more, but we keep it, because the /BatteryLife/State setting
	# this value.
	SocGuardLowSocCharge = 12

class Flags(object):
	Float = 0x01
	Absorption = 0x02
	Discharged = 0x04

class Constants(object):
	SocSwitchOffset = 3.0
	SocSwitchIncrement = 5.0
	SocSwitchDefaultMin = 10.0
	LowSocChargeOffset = 2.0 # Should be < SocSwitchOffset
	AbsorptionLevel = 85.0
	FloatLevel = 95.0
	SocSwitchMax = AbsorptionLevel - SocSwitchIncrement
	ForceChargeCurrent = 5.0
	ForceChargeInterval = 24 * 60 * 60 # 5 days

def bound(low, v, high):
	return max(low, min(v, high))

def dt_to_stamp(dt):
	""" Return UTC timestamp for datetime dt. """
	return (dt - datetime(1970, 1, 1)).total_seconds()

class BatteryLife(SystemCalcDelegate):
	""" Calculates the ESS CGwacs state. """

	# Items we want to track from systemcalc
	_tracked_attrs = {'soc': '/Dc/Battery/Soc', 'vebus': '/VebusService'}

	def __init__(self):
		super(BatteryLife, self).__init__()
		self._tracked_values = {}
		self._timer = gobject.timeout_add(900000, exit_on_error, self._on_timer)

	def get_input(self):
		# We need to check the assistantid to know if we should even be active.
		# We also need to check the sustain flag.
		return [
			('com.victronenergy.vebus', [
				'/Hub4/AssistantId',
				'/Hub4/Sustain']),
			('com.victronenergy.settings', [
				STATE_PATH, FLAGS_PATH, DISCHARGED_TIME_PATH,
				DISCHARGED_SOC_PATH, SOC_LIMIT_PATH, MIN_SOC_LIMIT_PATH])
		]

	def get_output(self):
		return []

	def get_settings(self):
		return [
			('state', STATE_PATH, 1, 0, 0),
			('flags', FLAGS_PATH, 0, 0, 0, 1),
			('dischargedtime', DISCHARGED_TIME_PATH, 0, 0, 0, 1),
			('dischargedsoc', DISCHARGED_SOC_PATH, -1, -1, 100, 1),
			('soclimit', SOC_LIMIT_PATH, 10.0, 0, 100, 1),
			('minsoclimit', MIN_SOC_LIMIT_PATH, 10.0, 0, 100),
		]

	_get_time = datetime.now

	@property
	def state(self):
		return self._settings['state']

	@state.setter
	def state(self, v):
		self._settings['state'] = v

	@property
	def flags(self):
		return self._settings['flags']

	@flags.setter
	def flags(self, v):
		self._settings['flags'] = v

	def _disabled(self):
		if self._dbusmonitor.get_value(self.vebus, '/Hub4/AssistantId') is not None:
			return State.BLRestart

	def _restart(self):
		# Do the same as in the default case
		return self._default(False)

	def _default(self, adjust=True):
		if self.sustain or (self.soc <= self.active_soclimit and self.soc < 100):
			return self.on_discharged(adjust)
		elif self.soc >= Constants.FloatLevel:
			return self.on_float(adjust)
		elif self.soc >= Constants.AbsorptionLevel:
			return self.on_absorption(adjust)

		# Remain in default state
		return State.BLDefault

	def _discharged(self):
		if not self.sustain and (self.soc > self.switch_on_soc or self.soc >= 100):
			return State.BLDefault

	def _forcecharge(self):
		if not self.sustain and (self.soc > self.active_soclimit or self.soc >= 100):
			self.dischargedtime = dt_to_stamp(self._get_time())
			return State.BLDischarged

	def _absorption(self):
		if self.sustain or (self.soc <= self.active_soclimit and self.soc < 100):
			return self.on_discharged(True)
		elif self.soc > Constants.FloatLevel:
			return self.on_float(True)
		elif self.soc < Constants.FloatLevel - Constants.SocSwitchOffset:
			return State.BLDefault

	def _float(self):
		if self.sustain or (self.soc <= self.active_soclimit and self.soc < 100):
			return self.on_discharged()
		elif self.soc < Constants.FloatLevel - Constants.SocSwitchOffset:
			return State.BLAbsorption

	def _socguard_default(self):
		if self.soc < 100 and self.soc <= self.minsoclimit:
			return State.SocGuardDischarged

	def _socguard_discharged(self):
		if self.soc >= 100 or (self.soc > self.minsoclimit + Constants.LowSocChargeOffset):
			return State.SocGuardDefault

	def adjust_soc_limit(self, delta):
		limit = max(self._settings['minsoclimit'],
			self._settings['soclimit']) + delta
		self._settings['soclimit'] = bound(0.0, limit, Constants.SocSwitchMax)

	def on_discharged(self, adjust):
		# set dischargedsoc to the active limit just before going to the
		# discharged state. If the soc drops further, we will recharge back
		# to this level.
		limit = self.active_soclimit
		if self.dischargedsoc < 0 or self.dischargedsoc > limit:
			self.dischargedsoc = limit

		if adjust:
			if not self.flags & Flags.Discharged:
				self.flags |= Flags.Discharged
				self.adjust_soc_limit(Constants.SocSwitchIncrement)
			self.dischargedtime = dt_to_stamp(self._get_time())
		return State.BLSustain if self.sustain else State.BLDischarged

	def on_absorption(self, adjust):
		if adjust and not self.flags & Flags.Absorption:
			self.flags |= Flags.Absorption
			self.adjust_soc_limit(-Constants.SocSwitchIncrement)
		return State.BLAbsorption

	def on_float(self, adjust):
		offset = 0
		flags = self.flags
		if adjust:
			if not (flags & Flags.Absorption):
				offset -= Constants.SocSwitchIncrement
				flags |= Flags.Absorption
			if not (flags & Flags.Float):
				offset -= Constants.SocSwitchIncrement
				flags |= Flags.Float
			self.flags = flags
			self.adjust_soc_limit(offset)
			return State.BLFloat

	_map = {
		State.BLDisabled: _disabled,
		State.BLRestart: _restart,
		State.BLDefault: _default,
		State.BLAbsorption: _absorption,
		State.BLFloat: _float,
		State.BLDischarged: _discharged,
		State.BLForceCharge: _forcecharge,
		State.BLSustain: _discharged,
		State.BLLowSocCharge: lambda s: State.BLDischarged,
		State.KeepCharged: lambda s: s.state,
		State.SocGuardDefault: _socguard_default,
		State.SocGuardDischarged: _socguard_discharged,
		State.SocGuardLowSocCharge: lambda s: State.SocGuardDischarged,
	}

	@property
	def sustain(self):
		return self._dbusmonitor.get_value(self.vebus, '/Hub4/Sustain')

	@property
	def soclimit(self):
		return self._settings['soclimit']

	@property
	def minsoclimit(self):
		return self._settings['minsoclimit']

	@property
	def active_soclimit(self):
		m = self._settings['minsoclimit']
		l = self._settings['soclimit']
		if m > Constants.SocSwitchMax:
			return m
		return bound(0, max(m, l), Constants.SocSwitchMax)

	@property
	def switch_on_soc(self):
		m = self._settings['minsoclimit']
		if m > Constants.SocSwitchMax:
			return m + Constants.LowSocChargeOffset
		return self.active_soclimit + Constants.SocSwitchOffset

	@property
	def dischargedtime(self):
		return self._settings['dischargedtime']

	@dischargedtime.setter
	def dischargedtime(self, v):
		self._settings['dischargedtime'] = int(v)

	@property
	def dischargedsoc(self):
		return self._settings['dischargedsoc']

	@dischargedsoc.setter
	def dischargedsoc(self, v):
		self._settings['dischargedsoc'] = v

	def __getattr__(self, k):
		""" Make our tracked values available as attributes, makes the
			code look neater. """
		try:
			return self._tracked_values[k]
		except KeyError:
			raise AttributeError(k)

	def update_values(self, newvalues):
		# Update tracked attributes
		for k, v in self._tracked_attrs.iteritems():
			self._tracked_values[k] = newvalues.get(v)

		# Cannot start without a multi or an soc
		if self.vebus is None or self.soc is None:
			logging.debug("[BatteryLife] No vebus or no valid SoC")
			return

		# Cannot start without ESS available
		if self._dbusmonitor.get_value(self.vebus, '/Hub4/AssistantId') is None:
			logging.debug("[BatteryLife] No ESS Assistant found")
			self.state = State.BLDisabled
			return

		newstate = self._map.get(self.state, lambda s: State.BLDefault)(self)
		if newstate is not None:
			self.state = newstate

	def _on_timer(self):
		now = self._get_time()

		# Test for the first 15-minute window of the day, and clear the flags
		if now.hour == 0 and now.minute < 15:
			self.flags = 0

		if self.state in (State.BLDischarged, State.BLSustain):
			# load dischargedtime, it's a unix timestamp, ie UTC
			if self.dischargedtime:
				dt = datetime.fromtimestamp(self.dischargedtime)
				if now - dt > timedelta(seconds=Constants.ForceChargeInterval):
					self.adjust_soc_limit(Constants.SocSwitchIncrement)
					self.state = State.BLForceCharge
			else:
				self.dischargedtime = dt_to_stamp(now)

		return True
