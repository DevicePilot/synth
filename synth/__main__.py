#!/usr/bin/env python
#
# Top-level module for the SYNTH project
# Generate and exercise synthetic devices for testing and demoing DevicePilot
#
# Copyright (c) 2017 DevicePilot Ltd.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from common import importer
import logging, math, time, sys, json, threading, subprocess, re, traceback
import random   # Might want to replace this with something we control
from datetime import datetime
from geo import geo
from wind import whitelees
import peopleNames
import sim
import ISO8601
import device
import zeromq_rx

params = {}

# Default params. Override these by specifying one or more JSON files on the command line.
params.update({
    "instance_name" : "default",    # Used for naming log files
    "initial_action" : None,
    "device_count" : 10,
    "start_time" : "now",
    "end_time" : None,
    "install_timespan" : sim.minutes(1),
    "battery_life_mu" : sim.minutes(5),
    "battery_life_sigma" : sim.minutes(1),
    "comms_reliability" : 1.0,  # Either a fractional number, or a specification string
    "web_key" : 12345,
    "web_response_min" : 3,  # (s) Range of delay to respond to an incoming web request
    "web_response_max" : 10
    })
    
def randList(start, delta, n):
    # Create a sorted list of <n> whole numbers ranging between <start> and <delta>
    L = [start + random.random()*delta for x in range(n)]
    return sorted(L)

def readParamfile(filename):
    try:
        s = open("scenarios/"+filename,"rt").read()
    except:
        s = open("../synth_accounts/"+filename,"rt").read()
    return s

def main():
    def createDevice(_):
        deviceNum = device.numDevices()
        (lon,lat) = pp.pickPoint()
        (firstName, lastName) = (peopleNames.firstName(deviceNum), peopleNames.lastName(deviceNum))
        firmware = random.choice(["0.51","0.52","0.6","0.6","0.6","0.7","0.7","0.7","0.7"])
        operator = random.choice(["O2","O2","O2","EE","EE","EE","EE","EE"])
        if operator=="O2":
            radioGoodness = 1.0-math.pow(random.random(), 2)    # Skewed towards 1
        else:
            radioGoodness = math.pow(random.random(), 2)        # Skewed towards 0
        props = {   "$id" : "-".join([format(random.randrange(0,255),'02x') for i in range(6)]), # A 6-byte MAC address 01-23-45-67-89-ab
                    "$ts" : sim.getTime1000(),
                    "is_demo_device" : True,    # A flag which lets us selectively delete later
                    "label" : "Thing "+str(deviceNum),
                    "longitude" : lon,
                    "latitude" : lat,
                    "first_name" : firstName,
                    "last_name" : lastName,
                    "full_name" : firstName + " " + lastName,
                    "factoryFirmware" : firmware,
                    "firmware" : firmware,
                    "operator" : operator,
                    "rssi" : ((1-radioGoodness)*(device.BAD_RSSI-device.GOOD_RSSI)+device.GOOD_RSSI),
                    "battery" : 100
                }
        client.add_device(props["$id"], props["$ts"], props)
        d = device.device(props["$id"], props["$ts"], props)
        if "comms_reliability" in params:
#            d.setCommsReliability(upDownPeriod=sim.days(0.5), reliability=1.0-math.pow(random.random(), 2)) # pow(r,2) skews distribution towards reliable end
            d.setCommsReliability(upDownPeriod=sim.days(0.5), reliability=params["comms_reliability"])
        d.setBatteryLife(params["battery_life_mu"], params["battery_life_sigma"], "battery_autoreplace" in params)

    def postWebEvent(webParams):    # CAUTION: Called asynchronously from the web server thread
        if "action" in webParams:
            if webParams["action"] == "event":
                if webParams["headers"]["Instancename"]==params["instance_name"]:
                    mini = float(params["web_response_min"])
                    maxi = float(params["web_response_max"])
                    sim.injectEventDelta(mini + random.random()*maxi, device.externalEvent, webParams)

    def enterInteractive():
        client.enter_interactive()

    logging.info("*** Synth starting at real time "+str(datetime.now())+" ***")
    
    for arg in sys.argv[1:]:
        if arg.startswith("{"):
            logging.info("Setting parameters "+arg)
            params.update(json.loads(arg))
        elif "=" in arg:    # RHS always interpreted as a string
            logging.info("Setting parameter "+arg)
            (key,value) = arg.split("=",1)  # split(,1) so that "a=b=c" means "a = b=c"
            params.update({ key : value })
        else:
            logging.info("Loading parameter file "+arg)
            s = readParamfile(arg)
            s = re.sub("#.*$","",s,flags=re.MULTILINE) # Remove Python-style comments
            params.update(json.loads(s))
    
    logging.info("Parameters:")
    for p in sorted(params):
        logging.info("    " + str(p) + " : " + str(params[p]))

    Tstart = time.time()
    random.seed(12345)  # Ensure reproduceability

    if not "client" in params:
        logging.error("No client defined")
        return
    client = importer.get_class('client', params['client']['type'])(params['client'])

    device.init(updatecallback=client.update_device, logfileName=params["instance_name"])

    zeromq_rx.init(postWebEvent)

    sim.init(enterInteractive)
    sim.setTimeStr(params.get("start_time", None), isStartTime=True)
    sim.setEndTimeStr(params.get("end_time", None))

    pp = geo.pointPicker()
    if "area_centre" in params:
        pp.setArea([params["area_centre"], params["area_radius"]])

    # Set up the world
    
##    if dp:
##        if params["initial_action"]=="deleteExisting":      # Recreate world from scratch
##            dp.deleteAllDevices()   # !!! TODO: Delete properties too.
##        if params["initial_action"]=="deleteDemo":  # Delete only demo devices (slow)
##            dp.deleteDevicesWhere('(is_demo_device == true)')
##        if params["initial_action"]=="loadExisting":       # Load existing world
##            for d in dp.getDevices():
##                device.device(d)
##    if aws:
##        if params["initial_action"] in ["deleteExisting", "deleteDemo"]:
##            aws.deleteDemoDevices()
##        # Loading device state from AWS not yet supported
##
##    if "device_setup" in params:
##        if params["device_setup"]=="whitelees":
##            whitelees.deviceSetup()
##        else:
##            print "Unrecognised device_setup:",params["device_setup"]
##            exit(-1)
##    else:
##        if params["initial_action"] != "loadExisting":
    sim.injectEvents(randList(sim.getTime(), params["install_timespan"], params["device_count"]), createDevice)

    logging.info("Simulation starts")
    try:
        while sim.eventsToCome():
            sim.nextEvent()
            client.tick()
    except:
        logging.error(traceback.format_exc()) # Report any exception, but continue to clean-up anyway

    device.flush()
    client.flush()
    logging.info("Simulation ends")

    logging.info("Elapsed real time: "+str(int(time.time()-Tstart))+" seconds")

if __name__ == "__main__":
    main()
