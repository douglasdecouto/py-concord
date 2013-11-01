#! /usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time

from concord import concord, concord_commands

# Note the "indigo" module is automatically imported and made available inside
# our global name space by the host process.


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.processPluginPrefs(pluginPrefs)


        # TODO: handle multiple partitions
        self.panel = None
        self.panelDev = None
        self.zones = { } # zone number -> dict of zone info, i.e. output of cmd_zone_data
        self.zoneDevs = { } # zone number -> dict of active zone devices

    def __del__(self):
        indigo.PluginBase.__del__(self)
        
    def log(self, msg):
        indigo.server.log(msg)
    def debug_msg(self, msg):
        indigo.server.log(msg)
    def error(self, msg):
        indigo.server.log(msg)

    def startup(self):
        self.debugLog(u"startup called")

    def shutdown(self):
        self.debugLog(u"shutdown called")
                

    # Plugin prefs methods
    def validatePrefsConfigUi(self, valuesDict):
        self.log("Validating prefs: %r" % (valuesDict))
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
        self.log("Closed prefs config...")
        if not userCancelled:
            self.processPluginPrefs(valuesDict)

    def processPluginPrefs(self, pluginPrefsDict):
        self.log("Loading plugin prefs...")
        self.serialPortUrl = self.getSerialPortUrl(pluginPrefsDict, 'panelSerialPort')        
        self.log("Serial port is: %s" % self.serialPortUrl)


    # Device methods

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.log("Validating Device...")

        return (True, valuesDict)

    def deviceStartComm(self, dev):
        self.log("Device start comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))
        if dev.deviceTypeId == "concordPanel":
            if self.panel is not None and self.panelDev.id != dev.id:
                self.error("Can't have more than one panel device; panel already setup at device id %r" % self.panelDev.id)
                raise Exception("Extra panel device")
            dev.updateStateOnServer(key='panelState', value='connecting')
            self.panelDev = dev
            self.panel = concord.AlarmPanelInterface(self.serialPortUrl, 0.5, self)
            self.panel_command_names = { } # code -> display-friendly name
            for code, cmd_info in concord_commands.RX_COMMANDS.iteritems():
                cmd_id, cmd_name = cmd_info[0], cmd_info[1]
                self.panel_command_names[cmd_id] = cmd_name
                self.panel.register_message_handler(cmd_id, self.panelMessageHandler)
            self.panel.request_all_equipment()
            self.panel.request_dynamic_data_refresh()
                
            localPropsCopy = dev.pluginProps
            localPropsCopy["dougTest"] = 10
            dev.replacePluginPropsOnServer(localPropsCopy)


        elif dev.deviceTypeId == 'zone':
            self.updateZoneDeviceState(dev)

        self.log("%s" % dev.states)

    def deviceStopComm(self, dev):
        self.log("Device stop comm: %s, %s, %s" % (dev.name, dev.id, dev.deviceTypeId))
        if dev.deviceTypeId == "concordPanel":
            if self.panel is None or self.panelDev.id != dev.id:
                self.error("Stopping a panel we don't know about at device id %r" % dev.id)
                raise Exception("Extra panel device")
            self.panel.stop_loop()
            self.panel = None
            self.panelDev = None


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
            self.log("Got StopThread in runConcurrentThread()")
            pass    


    # MenuItems.xml actions:
    def menuRefreshDynamicState(self):
        self.log("Menu item: Refresh Dynamic State")
        if not self.panel:
            self.log("No panel to refresh")
        else:
            self.panel.request_dynamic_data_refresh()

    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    def arm(self, pluginAction):
        self.debugLog("'arm' action called:\n" + str(pluginAction))

    # Device config callbacks & actions
    def zoneFilter(self, filter="", valuesDict=None, typeId="", targetId=0):
        """ Return list of zone numbers we have heard about. """
        return sorted(self.zones.keys())
        
    def updateZoneDeviceState(self, zone_dev):
        zone_num = zone_dev.states['zoneNumber']
        if zone_num not in self.zones:
            self.error("Unable to update Indigo zone device %s - %d; no knowledge of that zone" % \
                           (zone_dev.name, zone_num))
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

    # Will be run in the concurrent thread.
    def panelMessageHandler(self, msg):
        """ msg is dict with received message from the panel. """
        assert self.panelDev is not None
        cmd_id = msg['command_id']
        self.log("Plugin: handling panel message %s, %s" % \
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
                self.log("Updating zone %s with %s message" % (zone_name, cmd_id))
                zone_info = self.zones[zone_num]
                zone_info.update(msg)
                del zone_info['command_id']
            else:
                self.log("Learning new zone %s from %s message" % (zone_name, cmd_id))
                zone_info = msg.copy()
                del zone_info['command_id']
                self.zones[zone_num] = zone_info

            # Next sync up any Indigo devices that might be for this
            # zone.
            if zone_num in self.zoneDevs:
                self.updateZoneDeviceState(self.zoneDevs[zone_num])
        
            else:
                self.error("No Indigo zone device for zone %s" % zone_name)

        else:
            self.log("Plugin: unhandled panel message %s" % cmd_id)

    
