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

def zonekey(zoneDev):
    """ Return internal key for supplied Indigo zone device. """
    assert zoneDev.deviceTypeId == 'zone'
    return (int(zoneDev.pluginProps['partitionNumber']),
            int(zoneDev.pluginProps['zoneNumber']))
    
def partkey(partDev):
    """ Return internal key for supplied Indigo partition device. """
    assert partDev.deviceTypeId == 'partition'
    return int(partDev.address)


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.logger = Logger(lev=LOG_INFO)

        self.processPluginConfigPrefs(pluginPrefs)

        # TODO: handle multiple partitions
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

    #
    # Device methods
    #
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug("Validating %s device config..." % typeId)
        self.logger.debug("%s" % valuesDict) 
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
            if self.panel is not None and self.panelDev.id != dev.id:
                dev.updateStateOnServer('panelState', 'unknown')
                self.logger.error("Can't have more than one panel device; panel already setup at device id %r" % self.panelDev.id)
                return

            dev.updateStateOnServer('panelState', 'connecting')

            self.panelDev = dev
            try:
                self.panel = concord.AlarmPanelInterface(self.serialPortUrl, 0.5, self.logger)
            except Exception, ex:
                dev.updateStateOnServer("panelState", "error")
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

            # Ask the panel to tell us all about itself.
            dev.updateStateOnServer("panelState", "exploring")

            self.logger.debug("Querying panel for state")
            self.panel.request_all_equipment()
            self.panel.request_dynamic_data_refresh()
            
            self.panelInitialQueryDone = False

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

        else:
            raise Exception("Unknown device type: %r" % dev.deviceTypeId)


    def deviceStopComm(self, dev):
        self.logger.debug("Device stop comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))

        if dev.deviceTypeId == "panel":
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
            zk = zonekey(dev)
            if zk not in self.zoneDevs:
                self.logger.warn("Zone device %s - zone %d partition %d - is not known, ignoring" % \
                             (dev.name, zk[1], zk[0]))
                return
            known_dev = self.zoneDevs[zk]
            if dev.id != known_dev.id:
                self.logger.warn("Zone device id %d does not match id %d we already know about for zone %d, partition %d, ignoring" % (dev.id, known_dev.id, zk[1], zk[0]))
                return
            self.logger.debug("Deleteing zone dev %d" % dev.id)
            del self.zoneDevs[zk]

        elif dev.deviceTypeId == 'partition':
            pk = partkey(dev)

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
        Create Indigo Zone devices to match the devices in the panel.
        This function creates a new device if neccessary, but doesn't
        add it to our internal state; Indigo will call deviceStartComm
        on the device which gives us a chance to do that.
        """
        self.logger.debug("Creating Indigo Zone devices from panel data")
        for zk, zone_data in self.zones.iteritems():
            part_num, zone_num = zk
            zone_name = zone_data.get('zone_text', '')
            zone_type = zone_data.get('zone_type', '')
            if zone_type == '':
                zone_type = 'Unknown type'
            if zone_name == '':
                if zone_type == 'RF Touchpad':
                    zone_name = 'RF Touchpad - %d' % zone_num
                else:
                    zone_name = 'Unknown Zone - %d' % zone_num
            if zk not in self.zoneDevs:
                self.logger.info("Creating Zone %d, partition %d - %s" % (zone_num, part_num, zone_name))
                zone_dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, 
                                                address="%d/%d" % (zone_num, part_num),
                                                name=zone_name,
                                                description=zone_type,
                                                deviceTypeId="zone",
                                                props={'partitionNumber': part_num, 
                                                       'zoneNumber': zone_num})
            else:
                zone_dev = self.zoneDevs[zk]
                self.logger.info("Device %d already exists for Zone %d, partition %d - %s" % \
                                     (zone_dev.id, zone_num, part_num, zone_name))

                                    
    def menuDumpZonesToLog(self):
        """
        Print to log our iternal zone state information; cross-check
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


    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    def arm(self, pluginAction):
        self.logger.debug("'arm' action called:\n" + str(pluginAction))

    def partitionFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        """ Return list of zone numbers we have heard about. """
        return range(1, concord.CONCORD_MAX_ZONE+1)
        
    def updatePartitionDeviceState(self, part_dev, part_key):
        if part_key not in self.parts:
            self.logger.debug("Unable to update Indigo partition device %s - partition %d; no knowledge of that partition" % (part_dev.name, part_key))
            part_dev.updateStateOnServer('partitionState', 'unknown')
            return
        arm_level = self.parts[part_key].get('arming_level', 'Unknown arming level')
        state_map = { 'Off': 'ready',
                      'Stay': 'stay',
                      'Away': 'away',
                      'Phone Test': 'phone_test',
                      'Sensor Test': 'sensor_test',
                      }
        # TODO: How would we determine 'unready'?  Check that no zones are tripped?
        part_state = state_map.get(arm_level, 'unknown')
        part_dev.updateStateOnServer('partitionState', part_state)

        # some confusion between partition info command and arm level
        # command; they have different sets of valid arming states.
        # Need to map to some common set.

    def updateZoneDeviceState(self, zone_dev, zone_key):
        if zone_key not in self.zones:
            self.logger.debug("Unable to update Indigo zone device %s - zone %d partition %d; no knowledge of that zone" % (zone_dev.name, zone_key[1], zone_key[0]))
            zone_dev.updateStateOnServer('zoneState', 'unknown')
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
            part_num = msg['partition_number']
            zk = (part_num, zone_num)
            if 'zone_text' in msg and msg['zone_text'] != '':
                zone_name = '%s - %r' % (zone_num, msg['zone_text'])
            else:
                zone_name = '%d' % zone_num
            if zk in self.zones:
                self.logger.info("Updating zone %s with %s message" % (zone_name, cmd_id))
                zone_info = self.zones[zk]
                zone_info.update(msg)
                del zone_info['command_id']
            else:
                self.logger.info("Learning new zone %s from %s message" % (zone_name, cmd_id))
                zone_info = msg.copy()
                del zone_info['command_id']
                self.zones[zk] = zone_info

            # Next sync up any Indigo devices that might be for this
            # zone.
            if zk in self.zoneDevs:
                self.updateZoneDeviceState(self.zoneDevs[zk], zk)
            else:
                self.logger.warn("No Indigo zone device for zone %s" % zone_name)

        elif cmd_id == 'PART_DATA':
            part_num = msg['partition_number']
            if part_num in self.parts:
                self.logger.info("Updating partition %d with %s message" % (part_num, cmd_id))
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
                self.logger.warn("No Indigo partition device for partition %d" % part_num)

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

    
