"""
Main plugin driver code for Indigo plugin.
"""

import os
import sys
import time

from collections import deque
from datetime import datetime

from concord import concord, concord_commands, concord_alarm_codes
from concord.concord_commands import STAR, HASH

# Note: the "indigo" module is automatically imported and made
# available inside our global name space by the host process.

# Default length of internal event log, in days.  Note that this is
# the internal event log, as opposed to the logging of messages into
# the Indigo system log.
DEF_LOG_DAYS = 5
DEF_ERR_LOG_DAYS = 30


#
# Logging.  Roll our own because we want two levels of DEBUG.
#
LOG_ERR    = 0
LOG_WARN   = 1
LOG_INFO   = 2
LOG_DEBUG  = 3
LOG_DEBUGV = 4 # verbose debug

LOG_PREFIX = {
    LOG_ERR: "ERROR",
    LOG_WARN: "WARNING",
    LOG_INFO: "INFO",
    LOG_DEBUG: "DEBUG",
    LOG_DEBUGV: "DEBUG VERBOSE",
}

LOG_CONFIG = {
    'error': LOG_ERR,
    'warn': LOG_WARN,
    'info': LOG_INFO,
    'debug': LOG_DEBUG,
    'debugVerbose': LOG_DEBUGV,
}

class Logger(object):
    def __init__(self, lev=LOG_INFO):
        self.log_fn = indigo.server.log
        self.level = lev
    def set_level(self, lev):
        assert lev >= 0 and lev <= LOG_DEBUGV
        self.level = lev
    def log(self, msg, level=LOG_INFO):
        if level == LOG_ERR:
            indigo.server.log(msg, isError=True)
        elif level <= self.level:
            indigo.server.log("[%s] %s" % (LOG_PREFIX[level], msg))
    def error(self, msg): self.log(msg, level=LOG_ERR)
    def warn(self, msg): self.log(msg, level=LOG_WARN)
    def info(self, msg): self.log(msg, level=LOG_INFO)
    def debug(self, msg): self.log(msg, level=LOG_DEBUG)
    def debug_verbose(self, msg): self.log(msg, level=LOG_DEBUGV)
    def log_always(self, msg): indigo.server.log(msg)

def zonekey(zoneDev):
    """ Return internal key for supplied Indigo zone device. """
    assert zoneDev.deviceTypeId == 'zone'
    return (int(zoneDev.pluginProps['partitionNumber']),
            int(zoneDev.pluginProps['zoneNumber']))
    
def partkey(partDev):
    """ Return internal key for supplied Indigo partition or touchpad device. """
    assert partDev.deviceTypeId in ('partition', 'touchpad')
    return int(partDev.address)

def any_if_blank(s):
    if s == '': return 'any'
    else: return s


#
# Touchpad display when no data available
#
NO_DATA = '<NO DATA>'

#
# Keypad sequences for various actions
#
KEYPRESS_SILENT = [ 5 ]
KEYPRESS_ARM_STAY = [ 2 ]
KEYPRESS_ARM_AWAY = [ 3 ]
KEYPRESS_BYPASS = [ 0xb ] # '#'
KEYPRESS_TOGGLE_CHIME = [ 7, 1 ]

KEYPRESS_EXIT_PROGRAM = [ STAR, 0, 0, HASH ]




#
# XML configuration filters
# 
PART_FILTER = [(str(p), str(p)) for p in range(1, concord.CONCORD_MAX_ZONE+1)]
PART_FILTER_TRIGGER = [('any', 'Any')] + PART_FILTER

PART_STATE_FILTER = [ 
    ('unknown', 'Unknown'),
    ('ready', 'Ready'), # aka 'off'
    ('unready', 'Not Ready'), # Not actually a Concord state 
    ('zone_test', 'Phone Test'),
    ('phone_test', 'Phone Test'),
    ('sensor_test', 'Sensor Test'),
    ('stay', 'Armed Stay'),
    ('away', 'Armed Away'),
    ('night', 'Armed Night'),
    ('silent', 'Armed Silent'),
    ]
PART_STATE_FILTER_TRIGGER = [('any', 'Any')] + PART_STATE_FILTER

# Different messages (i.e. PART_DATA and ARM_LEVEL) may
# provide different sets of partitiion arming states; this dict
# unifies them and translates them to the states our Partitiion device
# supports.
PART_ARM_STATE_MAP = {
    # Original arming code -> Partition device state
    -1: 'unknown', # Internal to plugin
    0: 'zone_test', # 'Zone Test', ARM_LEVEL only
    1: 'ready', # 'Off',
    2: 'stay', # 'Home/Perimeter',
    3: 'away', # 'Away/Full',
    4: 'night', # 'Night', ARM_LEVEL only
    5: 'silent', # 'Silent', ARM_LEVEL only
    8: 'phone_test', # 'Phone Test', PART_DATA only
    9: 'sensor_test', # 'Sensor Test', PART_DATA only
}


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.logger = Logger(lev=LOG_INFO)

        self.processPluginConfigPrefs(pluginPrefs)

        self.panel = None
        self.panelDev = None
        self.panelInitialQueryDone = False
    
        # Zones are keyed by (partitition number, zone number)
        self.zones = { } # zone key -> dict of zone info, i.e. output of cmd_zone_data
        self.zoneDevs = { } # zone key -> active Indigo zone device
        self.zoneKeysById = { } # zone device ID -> zone key

        # Partitions are keyed by partition number
        self.parts = { } # partition number -> partition info
        self.partDevs = { } # partition number -> active Indigo partition device
        self.partKeysById = { } # partition device ID -> partition number
        
        # Touchpads don't actually have any of their own internal
        # data; they just mirror their configured partition.  To aid
        # that, we will attach touchpad display information to the
        # internal partition state.
        self.touchpadDevs = { } # partition number -> (touchpad device ID -> Indigo touchpad device)

        # Triggers are keyed by Indigo trigger ID; these are used to
        # fire off the events described in our Events.xml.
        self.triggers = { }

        # We maintain a regular event log, and an 'error' event log
        # with only exception-type information.  Each has an
        # associated number of days for which it retains events (from
        # oldest to most recent event in log).
        #
        # These are logs of events kep internally to the plugin, as
        # opposed to the log messages which are printed/sent to Indigo
        # and controlled by the 'log level'
        self.eventLog = deque()
        self.errLog = deque()
        self.eventLogDays = 0
        self.errLogDays = 0

        # If a reporting email address is specified, we will try to
        # send emails about exception events.
        self.reportEmail = None
        
    def __del__(self):
        indigo.PluginBase.__del__(self)

    def startup(self):
        self.logger.debug("startup called")
        self.logEvent("Plugin starting up", True)

    def shutdown(self):
        self.logger.debug("shutdown called")
        self.logEvent("Plugin stopping", True)

    #
    # Internal event log
    #
    def _logEvent(self, eventInfo, eventTime, q, maxAge):
        pair = (eventTime, eventInfo)
        q.append(pair)
        while len(q) > 0:
            dt = eventTime - q[0][0]
            if dt.days > maxAge:
                q.popleft()
            else:
                break

    def logEvent(self, eventInfo, isErr=False):
        event_time = datetime.now()
        self._logEvent(eventInfo, event_time, self.eventLog, self.eventLogDays)
        if isErr:
            self._logEvent(eventInfo, event_time, self.errLog, self.errLogDays)

    def logEventZone(self, zoneName, zoneState, prevZoneState, logMessage, cmd, cmdData, isErr=False):
        d = { 'zone_name': zoneName,
              'zone_state': zoneState,
              'prev_zone_state': prevZoneState,
              'message': logMessage,
              'command': cmd,
              'command_data': cmdData }
        self.logEvent(d, isErr)

    #
    # Triggers
    #
    def triggerStartProcessing(self, trigger):
        self.logger.debug("Adding Trigger %d - %s" % (trigger.id, trigger.name))
        assert trigger.id not in self.triggers
        self.triggers[trigger.id] = trigger
 
    def triggerStopProcessing(self, trigger):
        self.logger.debug("Removing Trigger %d - %s" % (trigger.id, trigger.name))
        assert trigger.id in self.triggers
        del self.triggers[trigger.id] 

    def getTriggersForType(self, triggerTypeIds):
        """ 
        *triggerTypeIds* is a set or list of trigger type IDs we want
        to check.  We will give back the list of those types of
        triggers we know about in a deterministic order.
        """
        t = [ ]
        for tid, trigger in sorted(self.triggers.iteritems()):
            if trigger.pluginTypeId in triggerTypeIds:
                t.append(trigger)
        return t

    #
    # Plugin prefs methods
    #
    def validatePrefsConfigUi(self, valuesDict):
        self.logger.debug("Validating prefs: %r" % (valuesDict))
        errorsDict = indigo.Dict()
        self.validateSerialPortUi(valuesDict, errorsDict, "panelSerialPort")
        
        # Put other config UI validation here -- add errors to errorDict.
        try: logDays = int(valuesDict['logDays'])
        except ValueError: logDays = -1
        if logDays < 0:
            errorsDict['logDays'] = "Log size must be integer >= 0 days"

        try: errLogDays = int(valuesDict['errLogDays'])
        except ValueError: errLogDays = -1
        if logDays < 0:
            errorsDict['errLogDays'] = "Error log size must be integer >= 0 days"

        email = valuesDict['reportEmail']
        if email.strip() != '' and '@' not in email:
            errorsDict['reportEmail'] = "Report email should be blank or a valid email address"

        if len(errorsDict) > 0:
            # Some UI fields are not valid, return corrected fields and error messages (client
            # will not let the dialog window close).
            return (False, valuesDict, errorsDict)

        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        self.logger.debug("Closed prefs config...")
        if userCancelled:
            return
        self.processPluginConfigPrefs(valuesDict)

    def processPluginConfigPrefs(self, pluginPrefsDict):
        self.logger.debug("Loading plugin prefs...")
        self.serialPortUrl = self.getSerialPortUrl(pluginPrefsDict, 'panelSerialPort')        
        self.logger.info("Serial port is: %s" % self.serialPortUrl)
        # Need to keep and reconfigure same logger object as we have
        # gone and passed it off to subsystems like the
        # AlarmPanelInterface.
        self.logger.set_level(LOG_CONFIG.get(pluginPrefsDict.get('logLevel', 'info'), LOG_INFO))
        self.keepAlive = pluginPrefsDict.get('keepAlive', False)
        self.logger.log_always("New prefs: Keep Alive=%r, Log Level=%s" % \
                                   (self.keepAlive, LOG_PREFIX[self.logger.level]))
        self.reportEmail = pluginPrefsDict.get('reportEmail', '')
        if self.reportEmail.strip() == '':
            self.reportEmail = None
        self.eventLogDays = int(pluginPrefsDict.get('logDays', DEF_LOG_DAYS))
        self.errLogDays = int(pluginPrefsDict.get('errLogDays', DEF_ERR_LOG_DAYS))

    #
    # Device methods
    #
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug("Validating %s device config..." % typeId)
        dev = indigo.devices[devId]
        errors = indigo.Dict()

        if typeId == 'panel':
            if self.panelDev is not None and self.panelDev.id != devId:
                errors['theLabel'] = "There can only be one panel device"
            valuesDict['address'] = self.serialPortUrl

        elif typeId == 'partition':
            try: address = int(valuesDict['address'])
            except ValueError: address = -1
            if address < 1 or address > concord.CONCORD_MAX_ZONE:
                errors['address'] = "Partition must be set to a valid value (1-%d)" % concord.CONCORD_MAX_ZONE
            if address in self.partDevs:
                errors['address'] = "Another partition device has the same number"

        elif typeId == 'touchpad':
            try: address = int(valuesDict['address'])
            except ValueError: address = -1
            if address < 1 or address > concord.CONCORD_MAX_ZONE:
                errors['address'] = "Partition must be set to a valid value (1-%d)" % concord.CONCORD_MAX_ZONE
            # We will let you multiple touchpads for the same
            # partition.  This may be a bit arbitrary but sort of
            # mirrors 'real life'

        elif typeId == 'zone':
            try: part = int(valuesDict['partitionNumber'])
            except ValueError: part = -1
            try: zone = int(valuesDict['zoneNumber'])
            except ValueError: zone = -1
            if part < 1 or part > concord.CONCORD_MAX_ZONE:
                errors['partitionNumber'] = "Partition must be set to a valid value (1-%d)" % concord.CONCORD_MAX_ZONE
            if zone < 1:
                errors['zoneNumber'] = "Zone must be greater than 0"
            if (part, zone) in self.zoneDevs:
                errors['zoneNumber'] = "Another zone device in this partition has the same number"
            valuesDict['address'] = "%d/%d" % (zone, part)

        else:
            raise Exception("Unknown device type %s" % typeId)
        if len(errors) > 0:
            return (False, valuesDict, errors)
        return (True, valuesDict)


    def deviceStartComm(self, dev):
        self.logger.debug("Device start comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

        if dev.deviceTypeId == "panel":
            self.logEvent("Starting panel device %r" % dev.name, True)
            if self.panel is not None and self.panelDev.id != dev.id:
                dev.updateStateOnServer('panelState', 'unavailable')
                self.logger.error("Can't have more than one panel device; panel already setup at device id %r" % self.panelDev.id)
                return

            dev.updateStateOnServer('panelState', 'connecting')

            self.panelDev = dev
            try:
                self.panel = concord.AlarmPanelInterface(self.serialPortUrl, 0.5, self.logger)
            except Exception, ex:
                dev.updateStateOnServer("panelState", "faulted")
                dev.setErrorStateOnServer("Unable to connect")
                self.logger.error("Unable to start alarm panel interface: %s" % str(ex))
                return

            # Set the plugin object to handle all incoming commands
            # from the panel via the messageHandler() method.
            self.panel_command_names = { } # code -> display-friendly name
            for code, cmd_info in concord_commands.RX_COMMANDS.iteritems():
                cmd_id, cmd_name = cmd_info[0], cmd_info[1]
                self.panel_command_names[cmd_id] = cmd_name
                self.panel.register_message_handler(cmd_id, self.panelMessageHandler)

            self.refreshPanelState("Indigo panel device startup")

        elif dev.deviceTypeId == 'zone':
            zk = zonekey(dev)
            if zk in self.zoneDevs:
                self.logger.warn("Zone device %s has a duplicate zone %d in partition %d, ignoring" % \
                                     (dev.name, zk[1], zk[0]))
                return
            self.zoneDevs[zk] = dev
            self.zoneKeysById[dev.id] = zk
            self.updateZoneDeviceState(dev, zk)

        elif dev.deviceTypeId == 'partition':
            pk = partkey(dev)
            if pk in self.partDevs:
                self.logger.warn("Partition device %s has a duplicate partition number %d, ignoring" % \
                               (dev.name, pk))
                return
            self.partDevs[pk] = dev
            self.updatePartitionDeviceState(dev, pk)

        elif dev.deviceTypeId == 'touchpad':
            pk = partkey(dev)
            if pk not in self.touchpadDevs:
                self.touchpadDevs[pk] = { }
            self.touchpadDevs[pk][dev.id] = dev
            self.updateTouchpadDeviceState(dev, pk)

        else:
            raise Exception("Unknown device type: %r" % dev.deviceTypeId)


    def deviceStopComm(self, dev):
        self.logger.debug("Device stop comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

        if dev.deviceTypeId == "panel":
            self.logEvent("Stopping panel device %r" % dev.name, True)
            if self.panelDev is None or self.panelDev.id != dev.id:
                self.logger.error("Stopping a panel we don't know about at device id %r" % dev.id)
                raise Exception("Extra panel device")
            # AlarmPanel object may never have been successfully
            # started (e.g. was unable to open serial port in the
            # first place).
            if self.panel is not None:
                self.panel.stop_loop()
            self.panel = None
            self.panelDev = None
            self.panelInitialQueryDone = False
            
        elif dev.deviceTypeId == "zone":
            zk = zonekey(dev)
            if zk not in self.zoneDevs:
                self.logger.warn("Zone device %s - zone %d partition %d - is not known, ignoring" % \
                             (dev.name, zk[1], zk[0]))
                return
            known_dev = self.zoneDevs[zk]
            if dev.id != known_dev.id:
                self.logger.warn("Zone device id %d does not match id %d we already know about for zone %d, partition %d, ignoring" % (dev.id, known_dev.id, zk[1], zk[0]))
                return
            self.logger.debug("Deleting zone dev %d" % dev.id)
            del self.zoneDevs[zk]

        elif dev.deviceTypeId == 'partition':
            pk = partkey(dev)
            if pk not in self.partDevs:
                self.logger.warn("Partition device %s - partition %d - is not known, ignoring" % (dev.name, pk))
                return
            known_dev = self.partDevs[pk]
            if dev.id != known_dev.id:
                self.logger.warn("Partition device id %d does not match id %d we already know about for partition %d, ignoring" % (dev.id, known_dev.id, pk))
                return
            self.logger.debug("Deleting partition dev %d" % dev.id)
            del self.partDevs[pk]

        elif dev.deviceTypeId == 'touchpad':
            pk = partkey(dev)
            if pk not in self.partDevs:
                self.logger.warn("Touchpad device %s - partition %d - is not known, ignoring" % (dev.name, pk))       
            if dev.id not in self.touchpadDevs[pk]:
                self.logger.warn("Touchpad device id %d is not known" % dev.id)
            else:
                del self.touchpadDevs[pk][dev.id]

        else:
            raise Exception("Unknown device type: %r" % dev.deviceTypeId)


    def runConcurrentThread(self):
        try:
            # Run the panel interface event loop.  It's possible for
            # this thread to be running before the panel object is
            # constructed and the serial port is configured.  We have
            # an outer loop because the user may stop the panel device
            # which will cause the panel's message loop to be stopped.
            while True:
                while self.panel is None:
                    self.sleep(1)
                self.panel.message_loop()

        except self.StopThread:
            self.logger.debug("Got StopThread in runConcurrentThread()")
            pass    

    def refreshPanelState(self, reason):
        """
        Ask the panel to tell us all about itself.  We do this on
        startup, and when the panel asks us to (e.g. under various
        error conditions, or even just periodically).
        """
        self.logger.info("Querying panel for state (%s)" % reason)
        if self.panelDev is None:
            self.logger.error("No Indigo panel device configured")
            return

        self.panelDev.updateStateOnServer("panelState", "exploring")
        self.panel.request_all_equipment()
        self.panel.request_dynamic_data_refresh()
        self.panelInitialQueryDone = False
        

    def isReadyToArm(self, partition_num):
        """ 
        Returns pair: first element is True if it's ok to arm;
        otherwise the first element is False and the second element is
        the (string) reason why it is not possible to arm.
        """
        if self.panel is None:
            return False, "The panel is not active"

        # TODO: check all the zones, etc.
        return True, "Partition ready to arm"

    def checkPartition(self, valuesDict, errorsDict):
        try:
            part = int(valuesDict['partition'])
        except ValueError: part = -1
        if part < 1 or part > concord.CONCORD_MAX_ZONE:
            errorsDict['partition'] = "Partition must be set to a valid value (1-%d)" % concord.CONCORD_MAX_ZONE
        return part

    # MenuItems.xml actions:
    def menuArmDisarm(self, valuesDict, itemId):
        self.logger.debug("Menu item: Arm/Disarm: %s" % str(valuesDict))

        errors = indigo.Dict()

        arm_silent = valuesDict['silent']
        bypass = valuesDict['bypass']
        action = valuesDict['action']

        self.logEvent("Menu Arm/Disarm to %s, bypass=%r, silent=%r" % (action, bypass, silent), True)

        part = self.checkPartition(valuesDict, errors)
        if part > 0:
            can_arm, reason = self.isReadyToArm(part)
            if not can_arm:
                errors['partition'] = reason

        if self.panel is None:
            errors['partition'] = "The alarm panel is not active"

        if len(errors) > 0:
            return False, valuesDict, errors

        keys = [ ]
        if arm_silent:
            keys += KEYPRESS_SILENT
    
        if action == 'stay':
            keys += KEYPRESS_ARM_STAY
        elif action == 'away':
            keys += KEYPRESS_ARM_AWAY
        else:
            assert False, "Unknown arming action type"

        if bypass:
            keys += KEYPRESS_BYPASS

        try:
            self.panel.send_keypress(keys, part)
        except Exception, ex:
            self.logger.error("Problem trying to arm action=%r, silent=%r, bypass=%r" % \
                                  (action, arm_silent, bypass))
            self.logger.error(str(ex))
            errors['partition'] = str(ex)
            return False, valuesDict, errors
        
        return True, valuesDict


    def strToCode(self, s):
        if len(s) != 4:
            raise ValueError("Too short, must be 4 characters")
        v = [ ]
        for c in s:
            n = ord(c) - ord('0')
            if n < 0 or n > 9:
                raise ValueError("Non-numeric digit")
            v += [ n ]
        return v

    def menuSetVolume(self, valuesDict, itemId):
        self.logger.debug("Menu item: Set volume: %s" % str(valuesDict))
        errors = indigo.Dict()

        part = self.checkPartition(valuesDict, errors)

        try: code_keys = self.strToCode(valuesDict['code'])
        except ValueError: errors['code'] = "User code must be four digits"

        try: 
            volume = int(valuesDict['volume'])
            if volume < 0 or volume > 7: raise ValueError()
        except ValueError:
            errors['volume'] = "Volume must be between 0 (off) and 7 inclusive"

        if self.panel is None:
            errors['partition'] = "The alarm panel is not active"

        if len(errors) > 0:
            return False, valuesDict, errors

        keys = [ 9 ] + code_keys + [ STAR, 0, 4, 4, volume, HASH ]
        keys += KEYPRESS_EXIT_PROGRAM

        try:
            self.panel.send_keypress(keys, part)
        except Exception, ex:
            self.logger.error("Problem trying to set volume")
            self.logger.error(str(ex))
            errors['volume'] = str(ex)
            return False, valuesDict, errors
        
        return True, valuesDict


    def menuRefreshDynamicState(self):
        self.logger.debug("Menu item: Refresh Dynamic State")
        if not self.panel:
            self.logger.warn("No panel to refresh")
        else:
            self.panel.request_dynamic_data_refresh()

    def menuRefreshAllEquipment(self):
        self.logger.debug("Menu item: Refresh Full Equipment List")
        if not self.panel:
            self.logger.warn("No panel to refresh")
        else:
            self.panel.request_all_equipment()

    def menuRefreshZones(self):
        self.logger.debug("Menu item: Refresh Zones")
        if not self.panel:
            self.logger.warn("No panel to refresh")
        else:
            self.panel.request_zones()


    def menuCreateZoneDevices(self, valuesDict, itemId):
        """
        Create Indigo Zone devices to match the devices in the panel.
        This function creates a new device if neccessary, but doesn't
        add it to our internal state; Indigo will call deviceStartComm
        on the device which gives us a chance to do that.
        """
        self.logger.debug("Creating Indigo Zone devices from panel data")
        use_title_case = valuesDict["useTitleCase"]
        prefix = valuesDict["prefix"]
        suffix = valuesDict["suffix"]
        self.logger.debug("   useTitleCase: %r" % use_title_case)
        self.logger.debug("   prefix: %r" % prefix)
        self.logger.debug("   suffix: %r" % suffix)

        self.logger.debug("Getting list of existing Indigo device names")
        device_names = set([d.name for d in indigo.devices])

        for zk, zone_data in self.zones.iteritems():
            part_num, zone_num = zk
            zone_name = zone_data.get('zone_text', '')
            if use_title_case:
                zone_name = zone_name.title()
            zone_type = zone_data.get('zone_type', '')
            # Fixup zone type names to be a bit more understandablle;
            # assume more people know what 'wireless' means rather than
            # 'RF'.
            if zone_type == '':
                zone_type = 'Unknown type'
            elif zone_type == 'RF':
                zone_type = 'Wireless Sensor'
            elif zone_type == 'RF Touchpad':
                zone_type = 'Wireless Keypad'
            if zone_name == '':
                if zone_type == 'Wireless Keypad':
                    zone_name = '%s - %d' % (zone_type, zone_num)
                else:
                    zone_name = 'Unknown Zone - %d' % zone_num

            # Add on user-specified prefix/suffix
            zone_name = prefix + zone_name + suffix
            unique_zone_name = zone_name
            
            # Check and ensure uniqueness against other Indigo device names.
            counter = 1
            while unique_zone_name in device_names:
                unique_zone_name = "%s %d" % (zone_name, counter)
                counter += 1
            
            if zk not in self.zoneDevs:
                self.logger.info("Creating Zone %d, partition %d - %s" % (zone_num, part_num, unique_zone_name))
                zone_dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, 
                                                address="%d/%d" % (zone_num, part_num),
                                                name=unique_zone_name,
                                                description=zone_type,
                                                deviceTypeId="zone",
                                                props={'partitionNumber': part_num, 
                                                       'zoneNumber': zone_num})
                # Because these are custom device types they are not
                # actually able to be shown in remote diplays like
                # Indigo Touch.
                # http://www.perceptiveautomation.com/userforum/viewtopic.php?f=22&t=7584&p=71344&hilit=custom+devices+in+remote+ui#p71344
                # indigo.device.displayInRemoteUI(zone_dev.id, value=True)
            else:
                zone_dev = self.zoneDevs[zk]
                self.logger.info("Device %d already exists for Zone %d, partition %d - %s" % \
                                     (zone_dev.id, zone_num, part_num, zone_dev.name))
        errors = indigo.Dict()
        return (True, valuesDict, errors)

    
    def menuDumpZonesToLog(self):
        """
        Print to log our internal zone state information; cross-check
        Indigo devices against this state.
        """
        for zk, zone_data in sorted(self.zones.iteritems()):
            part_num, zone_num = zk
            zone_name = zone_data.get('zone_text', 'Unknown')
            zone_type = zone_data.get('zone_type', 'Unknown')
            if zk in self.zoneDevs:
                indigo_id = self.zoneDevs[zk].id
            else:
                indigo_id = None
            self.logger.log_always("Zone %d, %s, Indigo device %r, state=%r, partition=%d, type=%s" % \
                                       (zone_num, zone_name,  indigo_id, zone_data['zone_state'],
                                        part_num, zone_type))

        for zk, dev in self.zoneDevs.iteritems():
            part_num, zone_num = zk
            if zk in self.zones:
                # We already know about this stone in our official
                # internal state.
                continue
            self.logger.log_always("No zone info for Indigo device %r, id=%d, state=%s, zone %d/%d" % \
                                       (dev.name, dev.id, dev.states['zoneState'], zone_num, part_num))


    def menuSendTestAlarm(self, valuesDict, itemId):
        errors = indigo.Dict()
        try:
            part = int(valuesDict['partition'])
        except ValueError: part = -1
        if part < 1 or part > concord.CONCORD_MAX_ZONE:
            errors['partition'] = "Partition must be set to a valid value (1-%d)" % concord.CONCORD_MAX_ZONE

        code_v = valuesDict['alarmCode'].split('.')
        CODE_ERR = "Alarm code must be two numbers (>0) separared by a dot, e.g. 3.21"
        if len(code_v) == 2:
            try:
                gen = int(code_v[0])
                spec = int(code_v[1])
                if gen < 0 or spec < 0:
                    raise ValueError()
            except ValueError:
                errors['alarmCode'] = CODE_ERR
        else:
            error['alarmCode'] = CODE_ERR

        self.logEvent("Menu Send Test Alarm %s, partition %d" % (valuesDict['alarmCode'], part), True)

        if self.panel is None:
            errors['partition'] = "The alarm panel is not active"

        if len(errors) > 0:
            return (False, valuesDict, errors)
        else:
            self.panel.inject_alarm_message(part, gen, spec)
            return (True, valuesDict)


    def menuClearLog(self, valuesDict, itemId):
        clear_event_log = valuesDict.get("clearLog", False)
        clear_error_log = valuesDict.get("clearErrLog", False)

        if clear_event_log:
            self.logger.log_always("Clearing Event log")
            self.eventLog.clear()
        if clear_error_log:
            self.logger.log_always("Clearing Error log")
            self.errLog.clear()

        return True, valuesDict

    def menuDumpLog(self, valuesDict, itemId):
        log_name = valuesDict.get("log", "none")
        if log_name == 'eventLog':
            log = self.eventLog
            name = 'Event log'
        elif log_name == 'errLog':
            log = self.errLog
            name = 'Error log'
        else:
            log = None

        self.logger.log_always("Displaying %s" % name)
        
        if log is not None:
            for t, entry in log:
                self.logger.log_always("%s: %r" % (t.isoformat(' '), entry))
        
        return True, valuesDict
        
    #
    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    #

    #
    # Helpers for XML config
    #
    def partitionFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        return PART_FILTER
    def partitionFilterForTriggers(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug("Enter PFT")
        return PART_FILTER_TRIGGER

    def partitionStateFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        return PART_STATE_FILTER
    def partitionStateFilterForTriggers(self, filter="", valuesDict=None, typeId="", targetId=0):
        return PART_STATE_FILTER_TRIGGER

    def alarmGeneralTypeFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        gen_codes = [ (str(gen_code), gen_name)
                      for gen_code, (gen_name, specific_map)
                      in sorted(concord_alarm_codes.ALARM_CODES.iteritems())]
        return [('any', 'Any')] + gen_codes

    def getPartitionState(self, part_key):
        assert part_key in self.parts
        part_data = self.parts[part_key]
        arm_level = part_data.get('arming_level_code', -1)
        part_state = PART_ARM_STATE_MAP.get(arm_level, 'unknown')
        return part_state
    
    def updateTouchpadDeviceState(self, touchpad_dev, part_key):
        if part_key not in self.parts:
            self.logger.debug("Unable to update Indigo touchpad device %s - partition %d; no knowledge of that partition" % (touchpad_dev.name, part_key))
            touchpad_dev.updateStateOnServer('partitionState', 'unknown')
            touchpad_dev.updateStateOnServer('lcdLine1', NO_DATA)
            touchpad_dev.updateStateOnServer('lcdLine2', NO_DATA)
            return

        part_data = self.parts[part_key]
        lcd_data = part_data.get('display_text', '%s\n%s' % (NO_DATA, NO_DATA))
        # Throw out the blink information.  Not sure how to handle it.
        lcd_data = lcd_data.replace('<blink>', '')
        lines = lcd_data.split('\n')
        if len(lines) > 0:
            touchpad_dev.updateStateOnServer('lcdLine1', lines[0].strip())
        else:
            touchpad_dev.updateStateOnServer('lcdLine1', NO_DATA)
        if len(lines) > 1:
            touchpad_dev.updateStateOnServer('lcdLine2', lines[1].strip())
        else:
            touchpad_dev.updateStateOnServer('lcdLine2', NO_DATA)
        touchpad_dev.updateStateOnServer('partitionState', self.getPartitionState(part_key))

    def updatePartitionDeviceState(self, part_dev, part_key):
        if part_key not in self.parts:
            self.logger.debug("Unable to update Indigo partition device %s - partition %d; no knowledge of that partition" % (part_dev.name, part_key))
            part_dev.updateStateOnServer('partitionState', 'unknown')
            part_dev.updateStateOnServer('armingUser', '')
            part_dev.updateStateOnServer('features', 'Unknown')
            part_dev.updateStateOnServer('delay', 'Unknown')
            return

        part_state = self.getPartitionState(part_key)
        part_data = self.parts[part_key]
        arm_user  = part_data.get('user_info', 'Unknown User')
        features  = part_data.get('feature_state', ['Unknown'])

        delay_flags = part_data.get('delay_flags')
        if not delay_flags:
            delay_str = "No delay info"
        else:
            delay_str = "%s, %d seconds" % (', '.join(delay_flags), part_data.get('delay_seconds', -1))

        # TODO: How would we determine 'unready'?  Check that no zones are tripped?
        part_dev.updateStateOnServer('partitionState', part_state)
        part_dev.updateStateOnServer('armingUser', arm_user)
        part_dev.updateStateOnServer('features', ', '.join(features))
        part_dev.updateStateOnServer('delay', delay_str)


    def updateZoneDeviceState(self, zone_dev, zone_key):
        if zone_key not in self.zones:
            self.logger.debug("Unable to update Indigo zone device %s - zone %d partition %d; no knowledge of that zone" % (zone_dev.name, zone_key[1], zone_key[0]))
            zone_dev.updateStateOnServer('zoneState', 'unavailable')
            return
        data = self.zones[zone_key]
        if 'zone_type' in data:
            zone_dev.updateStateOnServer('zoneType', data['zone_type'])
        if 'zone_text' in data:
            zone_dev.updateStateOnServer('zoneText', data['zone_text'])
        zone_state = data['zone_state']
        zone_dev.updateStateOnServer('isNormal', len(zone_state) == 0)
        zone_dev.updateStateOnServer('isTripped', 'Tripped' in zone_state)
        zone_dev.updateStateOnServer('isFaulted', 'Faulted' in zone_state)
        zone_dev.updateStateOnServer('isAlarm', 'Alarm' in zone_state)
        zone_dev.updateStateOnServer('isTrouble', 'Trouble' in zone_state)
        zone_dev.updateStateOnServer('isBypassed', 'Bypassed' in zone_state)

        # Update the summary zoneState.  See Devices.xml to understand
        # how we map multiple state flags into a single state that
        # Indigo understands.
        bypassed = 'Bypass' in zone_state
        if len(zone_state) == 0:
            zs = 'enabled'
        elif 'Faulted' in zone_state or 'Trouble' in zone_state:
            zs = 'faulted'
        elif 'Alarm' in zone_state:
            zs = 'alarm'
        elif 'Tripped' in zone_state:
            zs = 'tripped'
        elif 'Bypassed' in zone_state:
            zs = 'disabled'
        else:
            zs = 'unavailable'
            
        # if bypassed and zs in ('normal', 'tripped'):
        #     zs += '_bypassed'

        zone_dev.updateStateOnServer('zoneState', zs)
        if zs in ('faulted', 'alarm'):
            zone_dev.setErrorStateOnServer(', '.join(zone_state))


    # Will be run in the concurrent thread.
    def panelMessageHandler(self, msg):
        """ *msg* is dict with received message from the panel. """
        assert self.panelDev is not None
        cmd_id = msg['command_id']

        # Log about the message, but not for the ones we hear all the
        # time.  Chatterbox!
        if cmd_id in ('TOUCHPAD', 'SIREN_SYNC'):
            # These message come all the time so only print about them
            # if the user signed up for extra verbose debug logging.
            log_fn = self.logger.debug_verbose
        else:
            log_fn = self.logger.debug
        log_fn("Handling panel message %s, %s" % \
                   (cmd_id, self.panel_command_names.get(cmd_id, 'Unknown')))

        #
        # First set of cases by message to update plugin and device state.
        #
        if cmd_id == 'PANEL_TYPE':
            self.panelDev.updateStateOnServer('panelType', msg['panel_type'])
            self.panelDev.updateStateOnServer('panelIsConcord', msg['is_concord'])
            self.panelDev.updateStateOnServer('panelSerialNumber', msg['serial_number'])
            self.panelDev.updateStateOnServer('panelHwRev', msg['hardware_revision'])
            self.panelDev.updateStateOnServer('panelSwRev', msg['software_revision'])

        elif cmd_id in ('ZONE_DATA', 'ZONE_STATUS'):
            # First update our internal state about the zone
            zone_num = msg['zone_number']
            part_num = msg['partition_number']
            zk = (part_num, zone_num)
            if 'zone_text' in msg and msg['zone_text'] != '':
                zone_name = '%s - %r' % (zone_num, msg['zone_text'])
            elif zk in self.zones and self.zones[zk].get('zone_text', '') != '':
                zone_name = '%s - %r' % (zone_num, self.zones[zk]['zone_text'])
            else:
                zone_name = '%d' % zone_num

            old_zone_state = "Not known"
            new_zone_state = msg['zone_state']

            if zk in self.zones:
                self.logger.info("Updating zone %s with %s message, zone state=%r" % \
                                     (zone_name, cmd_id, msg['zone_state']))
                zone_info = self.zones[zk]
                old_zone_state = zone_info['zone_state']
                zone_info.update(msg)
                del zone_info['command_id']
            else:
                self.logger.info("Learning new zone %s from %s message, zone_state=%r" % \
                                     (zone_name, cmd_id, msg['zone_state']))
                zone_info = msg.copy()
                del zone_info['command_id']
                self.zones[zk] = zone_info

            # Next sync up any Indigo devices that might be for this
            # zone.
            if zk in self.zoneDevs:
                self.updateZoneDeviceState(self.zoneDevs[zk], zk)
            else:
                self.logger.warn("No Indigo zone device for zone %s" % zone_name)

            # Log to internal event log.  If the zone is changed to or
            # from one of the 'error' states, we will use the error
            # log as well.
            use_err_log = False
            for err_state in ['Alarm', 'Trouble', 'Bypassed']:
                if err_state in new_zone_state or err_state in old_zone_state:
                    use_err_log = True
            self.logEventZone(zone_name, new_zone_state, old_zone_state,
                              "Zone update message", cmd_id, msg, use_err_log)
            

        elif cmd_id in ('PART_DATA', 'ARM_LEVEL', 'FEAT_STATE', 'DELAY', 'TOUCHPAD'):
            part_num = msg['partition_number']
            old_part_state = "Unknown"
            if part_num in self.parts:
                old_part_state = self.getPartitionState(part_num)
                # Log informational message about updating the
                # partition with message info.  However, for touchpad
                # messages this could be quite frequent (every minute)
                # so log at a higher level.
                if cmd_id == 'TOUCHPAD':
                    log_fn = self.logger.debug_verbose
                else:
                    log_fn = self.logger.info
                log_fn("Updating partition %d with %s message" % (part_num, cmd_id))
                part_info = self.parts[part_num]
                part_info.update(msg)
                del part_info['command_id']
            else:
                self.logger.info("Learning new partition %d from %s message" % (part_num, cmd_id))
                part_info = msg.copy()
                del part_info['command_id']
                self.parts[part_num] = part_info

            if part_num in self.partDevs:
                self.updatePartitionDeviceState(self.partDevs[part_num], part_num)
            else:
                # The panel seems to send touchpad date/time messages
                # for all partitions it supports.  User may not wish
                # to see warnings if they haven't setup the Partition
                # device in Indigo, so log this at a higher level.
                if cmd_id == 'TOUCHPAD':
                    log_fn = self.logger.debug_verbose
                else:
                    log_fn = self.logger.warn
                log_fn("No Indigo partition device for partition %d" % part_num)

            # We update the touchpad even when it's not a TOUCHPAD
            # message so that the touchpad device can track the
            # underlying partition state.  Later on we may also add
            # other features to mirror the LEDs on an actual touchpad
            # as well.
            if part_num in self.touchpadDevs:
                for dev_id, dev in self.touchpadDevs[part_num].iteritems():
                    self.updateTouchpadDeviceState(dev, part_num)

            # Write message to internal log
            if cmd_id in ('PART_DATA', 'ARM_LEVEL', 'DELAY'):
                part_state = self.getPartitionState(part_num)
                use_err_log = cmd_id != 'PART_DATA' or old_part_state != part_state or part_state != 'ready'
                self.logEvent(msg, use_err_log)


        elif cmd_id == 'EQPT_LIST_DONE':
            if not self.panelInitialQueryDone:
                self.panelDev.updateStateOnServer('panelState', 'active')
                self.panelInitialQueryDone = True

        elif cmd_id == 'ALARM':
            # Update partition alarm states.
            #
            # XXX Set partitionState to 'alarm'?  Then need to track
            # state as it changes...  How to determine partition alarm
            # state when we first start up?  I know this will be a
            # rare case, but... Probably can say partition is in alarm
            # if any of its zones are in alarm.
            part_num = msg['partition_number']
            source_type = msg['source_type']
            source_num = msg['source_number']
            alarm_code_str ="%d.%d" % (msg['alarm_general_type_code'], msg['alarm_specific_type_code'])
            alarm_desc = "%s / %s" % (msg['alarm_general_type'], msg['alarm_specific_type'])
            event_data = msg['event_specific_data']

            self.logger.error("ALARM or TROUBLE on partition %d: Source is %s/%d; Alarm/Trouble is %s: %s; event data = %s" % (part_num, source_type, source_num, alarm_code_str, alarm_desc, event_data))

            # Try to get a better name for the alarm source if it is a zone.
            zk = (part_num, source_num)
            if source_type == 'Zone' and zk in self.zones:
                zone_name = self.zones[zk].get('zone_text', 'Unknown')
                if zk in self.zoneDevs:
                    source_desc = "Zone %d - Indigo zone %s, alarm zone %s" % \
                        (source_num, self.zoneDevs[zk].name, zone_name)
                else:
                    source_desc = "Zone %d - alarm zone %s" % (source_num, zone_name)
            else:
                source_desc = "%s, number %d" % (source_type, source_num)
            self.logger.error("ALARM or TROUBLE on partition %d: Source details: %s" % (part_num, source_desc))

            if part_num in self.partDevs:
                partDev = self.partDevs[part_num]
                self.logger.debug("Updating Indigo partition device %d" % partDev.id)
                partDev.updateStateOnServer('alarmSource', source_desc)
                partDev.updateStateOnServer('alarmCode', alarm_code_str)
                partDev.updateStateOnServer('alarmDescription', alarm_desc)
                partDev.updateStateOnServer('alarmEventData', event_data)
                self.logger.debug(" .... Done")
            else:
                self.logger.warn("No Indigo partition device for partition %d" % part_num)
            
            msg['source_desc'] = source_desc
            self.logEvent(msg, True)

        elif cmd_id in ('CLEAR_IMAGE', 'EVENT_LOST'):
            self.refreshPanelState("Reacting to %s message" % cmd_id)

        else:
            self.logger.debug_verbose("Plugin: unhandled panel message %s" % cmd_id)

        #
        # Second set of cases for trigger handling
        #
        if cmd_id == 'ARM_LEVEL':
            # Execute all arming level triggers that match this
            # message's partition and arming level.
            for trigger in self.getTriggersForType(['armingLevel']):
                part_num = msg['partition_number']
                arm_level = PART_ARM_STATE_MAP.get(msg['arming_level_code'], 'unknown')

                trig_part = any_if_blank(trigger.pluginProps['address'])
                trig_level = any_if_blank(trigger.pluginProps['partitionState'])

                part_match = (trig_part == 'any') or (int(trig_part) == part_num)
                level_match = (trig_level == 'any') or (trig_level == arm_level)
                
                if part_match and level_match:
                    indigo.trigger.execute(trigger)

        elif cmd_id == 'ALARM':
            for trigger in self.getTriggersForType(['alarm']):
                part_num = msg['partition_number']
                alarm_gen_code = msg['alarm_general_type_code']

                trig_part = any_if_blank(trigger.pluginProps['address'])
                trig_gen_code = any_if_blank(trigger.pluginProps['alarmGeneralType'])

                part_match = (trig_part == 'any') or (int(trig_part) == part_num)
                code_match = (trig_gen_code == 'any') or (int(trig_gen_code) == alarm_gen_code)
                
                if part_match and code_match:
                    indigo.trigger.execute(trigger)

        elif cmd_id == 'ZONE_STATUS':
            for trigger in self.getTriggersForType('zoneStateChanged'):
                pass
