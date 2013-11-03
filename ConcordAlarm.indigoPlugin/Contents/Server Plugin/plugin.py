"""

"""


import os
import sys
import time

from concord import concord, concord_commands

# Note the "indigo" module is automatically imported and made available inside
# our global name space by the host process.

# Logging.  Roll our own because we want two levels of DEBUG.
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

class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.logger = Logger(lev=LOG_INFO)

        self.processPluginConfigPrefs(pluginPrefs)

        # TODO: handle multiple partitions
        self.panel = None
        self.panelDev = None
        self.panelInitialQueryDone = False
        self.zones = { } # zone number -> dict of zone info, i.e. output of cmd_zone_data
        self.zoneDevs = { } # zone number -> dict of active zone devices


    def __del__(self):
        indigo.PluginBase.__del__(self)

    def startup(self):
        self.logger.debug("startup called")

    def shutdown(self):
        self.logger.debug("shutdown called")
                
    #
    # Plugin prefs methods
    #
    def validatePrefsConfigUi(self, valuesDict):
        self.logger.debug("Validating prefs: %r" % (valuesDict))
        errorsDict = indigo.Dict()
        self.validateSerialPortUi(valuesDict, errorsDict, "panelSerialPort")
        
        # Put other config UI validation here -- add errors to errorDict.
        # ...

        if len(errorsDict) > 0:
            # Some UI fields are not valid, return corrected fields and error messages (client
            # will not let the dialog window close).
            return (False, valuesDict, errorsDict)

        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        self.logger.debug("Closed prefs config...")
        if not userCancelled:
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

    #
    # Device methods
    #
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug("Validating Device...")
        return (True, valuesDict)

    def deviceStartComm(self, dev):
        self.logger.debug("Device start comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

        if dev.deviceTypeId == "concordPanel":
            if self.panel is not None and self.panelDev.id != dev.id:
                self.logger.error("Can't have more than one panel device; panel already setup at device id %r" % self.panelDev.id)
                raise Exception("Extra panel device")

            dev.updateStateOnServer(key='panelState', value='connecting')

            self.panelDev = dev
            try:
                self.panel = concord.AlarmPanelInterface(self.serialPortUrl, 0.5, self.logger)
            except Exception, ex:
                dev.updateStateOnServer("panelState", "error")
                dev.setErrorStateOnServer("Unable to connect")
                raise

            # Set the plugin object to handle all incoming commands
            # from the panl via the messageHandler() method.
            self.panel_command_names = { } # code -> display-friendly name
            for code, cmd_info in concord_commands.RX_COMMANDS.iteritems():
                cmd_id, cmd_name = cmd_info[0], cmd_info[1]
                self.panel_command_names[cmd_id] = cmd_name
                self.panel.register_message_handler(cmd_id, self.panelMessageHandler)

            # Ask the panel to tell us all about itself.
            dev.updateStateOnServer("panelState", "exploring")

            self.logger.debug("Querying panel for state")
            self.panel.request_all_equipment()
            self.panel.request_dynamic_data_refresh()
            
            self.panelInitialQueryDone = False

        elif dev.deviceTypeId == 'zone':
            zone_num = dev.states['zoneNumber']
            if zone_num in self.zoneDevs:
                self.logger.warn("Zone device %s has a duplicate zone number %d, ignoring" % \
                               (dev.name, zone_num))
                return
            self.zoneDevs[zone_num] = dev
            self.updateZoneDeviceState(dev)


    def deviceStopComm(self, dev):
        self.logger.debug("Device stop comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

        if dev.deviceTypeId == "concordPanel":
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
            
        elif dev.deviceTypeId == "zone":
            zone_num = dev.states['zoneNumber']
            if zone_num not in self.zoneDevs:
                self.logger.warn("Zone device %d - %s is not known, ignoring" % \
                             (zone_num, dev.name))
                return
            known_dev = self.zoneDevs[zone_num]
            if dev.id != known_dev.id:
                self.logger.warn("Zone device id %d does not match id %d we already know about for zone %d, ignoring" % (dev.id, known_dev.id, zone_num))
                return
            del self.zoneDevs[zone_num]
            

    #def getDeviceStateList(self, dev):
    #    self.log("Get Dev State List: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))
    #
    #def getDeviceDisplayStateId(self, dev):
    #    self.log("Get Dev Display State Id: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

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


    # MenuItems.xml actions:
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
        self.logger.debuig("Menu item: Refresh Zones")
        if not self.panel:
            self.logger.warn("No panel to refresh")
        else:
            self.panel.request_zones()

    def menuCreateZoneDevices(self):
        """
        Create or update Indigo Zone devices to match the devices in
        the panel.
        """
        self.logger.debug("Creating Indigo Zone devices from panel data")
        for zone_num, zone_data in self.zones.iteritems():
            zone_name = zone_data.get('zone_text', '')
            zone_type = zone_data.get('zone_type', '')
            if zone_type == '':
                zone_type = 'Unknown type'
            if zone_name == '':
                if zone_type == 'RF Touchpad':
                    zone_name = 'RF Touchpad - %d' % zone_num
                else:
                    zone_name = 'Unknown Zone - %d' % zone_num
            if zone_num not in self.zoneDevs:
                self.logger.info("Creating Zone %d - %s" % (zone_num, zone_name))
                zone_dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, 
                                                address="%d" % zone_num,
                                                name=zone_name,
                                                description=zone_type,
                                                deviceTypeId="zone")
                self.zoneDevs[zone_num] = zone_dev
                zone_dev.updateStateOnServer('zoneNumber', zone_num)
            else:
                self.logger.info("Updating Zone %d - %s" % (zone_num, zone_name))
                zone_dev = self.zoneDevs[zone_num]
                dev.name = zone_name
                dev.description = zone_type
        self.updateZoneDeviceState(zone_dev)
        
                                    
    def menuDumpZonesToLog(self):
        """
        Print to log our iternal zone state information; cross-check
        Indigo devices against this state.
        """
        for zone_num, zone_data in self.zones.iteritems():
            zone_name = zone_data.get('zone_text', 'Unknown')
            zone_type = zone_data.get('zone_type', 'Unknown')
            if zone_num in self.zoneDevs:
                indigo_id = self.zoneDevs[zone_num].id
            else:
                indigo_id = None
            self.logger.log_always("Zone %d, %s, Indigo device %r, state=%r, partition=%d, type=%s" % \
                                       (zone_num, zone_name,  indigo_id, zone_data['zone_state'],
                                        zone_data['partition_number'], zone_type))

        for zone_num, dev in self.zoneDevs.iteritems():
            if zone_num in self.zones:
                # We already know about this stone in our official
                # internal state.
                continue
            self.logger.log_always("No zone info for Indigo device %r, id=%d, state=%s" % \
                                       (dev.name, dev.id, dev.states['zoneState']))


    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    def arm(self, pluginAction):
        self.logger.debug("'arm' action called:\n" + str(pluginAction))

    # Device config callbacks & actions
    def zoneFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        """ Return list of zone numbers we have heard about. """
        return sorted(self.zones.keys())
        
    def updateZoneDeviceState(self, zone_dev):
        zone_num = zone_dev.states['zoneNumber']
        if zone_num not in self.zones:
            self.logger.debug("Unable to update Indigo zone device %s - %d; no knowledge of that zone" % \
                                 (zone_dev.name, zone_num))
            zone_dev.updateStateOnServer('zoneState', 'unknown')
            return
        data = self.zones[zone_num]
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

        # Update the summary zoneState
        bypassed = 'Bypass' in zone_state
        if len(zone_state) == 0:
            zs = 'normal'
        elif 'Alarm' in zone_state:
            zs = 'alarm'
        elif 'Faulted' in zone_state or 'Trouble' in zone_state:
            zs = 'fault'
        elif 'Tripped' in zone_state:
            zs = 'tripped'
        elif 'Bypassed' in zone_state:
            zs = 'bypassed'
        else:
            zs = 'unknown'
            
        if bypassed and zs in ('normal', 'tripped'):
            zs += '_bypassed'

        zone_dev.updateStateOnServer('zoneState', zs)
        if zs in ('fault', 'alarm'):
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

        if cmd_id == 'PANEL_TYPE':
            self.panelDev.updateStateOnServer('panelType', msg['panel_type'])
            self.panelDev.updateStateOnServer('panelIsConcord', msg['is_concord'])
            self.panelDev.updateStateOnServer('panelSerialNumber', msg['serial_number'])
            self.panelDev.updateStateOnServer('panelHwRev', msg['hardware_revision'])
            self.panelDev.updateStateOnServer('panelSwRev', msg['software_revision'])

        elif cmd_id in ('ZONE_DATA', 'ZONE_STATUS'):
            # First update our internal state about the zone
            zone_num = msg['zone_number']
            if 'zone_text' in msg and msg['zone_text'] != '':
                zone_name = '%s - %r' % (zone_num, msg['zone_text'])
            else:
                zone_name = '%d' % zone_num
            if zone_num in self.zones:
                self.logger.info("Updating zone %s with %s message" % (zone_name, cmd_id))
                zone_info = self.zones[zone_num]
                zone_info.update(msg)
                del zone_info['command_id']
            else:
                self.logger.info("Learning new zone %s from %s message" % (zone_name, cmd_id))
                zone_info = msg.copy()
                del zone_info['command_id']
                self.zones[zone_num] = zone_info

            # Next sync up any Indigo devices that might be for this
            # zone.
            if zone_num in self.zoneDevs:
                self.updateZoneDeviceState(self.zoneDevs[zone_num])
            else:
                self.logger.warn("No Indigo zone device for zone %s" % zone_name)

        elif cmd_id == 'EQPT_LIST_DONE':
            if not self.panelInitialQueryDone:
                self.panelDev.updateStateOnServer('panelState', 'ready')
                self.panelInitialQueryDone = True

        elif cmd_id == 'ALARM':
            # XXX how to reset from this state?
            # What other actions?  Triggers I think...
            self.panelDev.updateStateOnServer('panelState', 'alarm')

        elif cmd_id == 'ARM_LEVEL':
            # 
            pass

        elif cmd_id == 'CLEAR_IMAGE':
            # Logic goes here to empty all of our state and reload it.
            pass

        else:
            self.logger.debug_verbose("Plugin: unhandled panel message %s" % cmd_id)

    
