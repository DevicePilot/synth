"""
disruptive
=====
Simulates sensors from Disruptive Technologies.
By default they pair-up in "sites"

Configurable parameters::

    {
        "sensor_type" : (optional) one of [ccon, temperature, proximity, touch] - if not set then automatically chooses (temperature, proximity) alternately
        "site_prefix" : (optional)      "Fridge "#
        "send_network_status" : False   Disruptive sensors send a lot of these messages
    }

Device properties created::

    {
        "sensorType" : one of      [ccon, temperature, proximity, touch]
        "signalStrengthCellular" :   y                                    0..100
        "signalStrengthSensor" :               y           y        y
        "state" :                                          y              PRESENT | NOT_PRESENT
        "temperature" :                        y                          degrees C (0.00,0.05) every 15m
        "transmissionMode" :                   y           y        y     HIGH_POWER_BOOST_MODE | LOW_POWER_STANDARD_MODE
        "eventType" :                1         2           2        2     1="cellularStatus" every 5m  2="networkStatus" every 15m
        "connection" :               y                                    CELLULAR | OFFLINE

        "site"  # A site is something like a room, or a fridge, around which multiple devices can exist, whose behaviour is correlated
    }


    sensorType                     eventType              other properties
    ----------                     ---------              ----------------
    ccon
        every 5m                   "cellularStatus"       "signalStrengthCellular": 10
        when goes offline/online   "connectionStatus"     "connection" : CELLULAR|OFFLINE

    temperature/proximity/touch
        once a day                  batteryStatus         "batteryPercentage" : 100      
        every 15m                   networkStatus         "signalStrengthSensor": 0..100, "

    +temperature
        every 15m                   temperature           "temperature" : 22.5

    +proximity
        when changed                objectPresent         "state" : "PRESENT|NOT_PRESENT"     

    +touch
                                    touch 

    (interestingly, DT's temperature and proximity sensors both also report touch events, but we ignore that here)

"""
import random
import logging
import time
from math import sin, pi

from device import Device
from helpers.solar import solar
import device_factory

MINS = 60
HOURS = MINS * 60
DAYS = HOURS * 24 

CELLULAR_INTERVAL    = 5 * MINS
NETWORK_INTERVAL     = 15 * MINS
TEMPERATURE_INTERVAL = 15 * MINS
BATTERY_INTERVAL     = 1 * DAYS

INTERNAL_TEMP_C = -18
EXTERNAL_TEMP_C = 20
TEMP_COUPLING = 0.02    # How quickly internal temperature adapts to external temperature (this happens asymptotically, i.e. as TEMP_COUPLING fraction of the difference per TEMPERATURE_INTERVAL)

AV_DOOR_OPEN_MIN_TIME_S = 1 * MINS
AV_DOOR_OPEN_SIGMA_S = 2 * MINS
AV_DOOR_OPENS_PER_HOUR = 4
CHANCE_OF_DOOR_LEFT_OPEN = 500  # 1 in N openings causes door to be left open for a LONG time


COOLING_FAILURE_POSSIBLE = False    # e.g. compressor or power failure (unrelated to door events)
COOLING_FAILURE_MTBF = 100 * DAYS
COOLING_FAILURE_AV_TIME_TO_FIX = 3 * DAYS

#      0   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15  16  17  18  19  20  21  22  23  <- HOURS OF DAY
Mon = [0,  0,  0,  0,  0,  0,  0,  0,  1,  9,  7,  7,  5,  7,  6,  5,  6,  7,  1,  0,  0,  0,  0,  0] 
Tue = [0,  0,  0,  0,  0,  0,  0,  0,  1,  9,  7,  7,  5,  7,  6,  5,  6,  7,  1,  0,  0,  0,  0,  0] 
Wed = [0,  0,  0,  0,  0,  0,  0,  0,  1,  9,  7,  7,  5,  7,  6,  5,  6,  7,  1,  0,  0,  0,  0,  0] 
Thu = [0,  0,  0,  0,  0,  0,  0,  0,  1,  9,  7,  7,  5,  7,  6,  5,  6,  7,  1,  0,  0,  0,  0,  0] 
Fri = [0,  0,  0,  0,  0,  0,  0,  0,  1,  9,  7,  7,  5,  7,  6,  5,  6,  7,  1,  0,  0,  0,  0,  0] 
Sat = [0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0]
Sun = [0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0,  0]

normal_office_hours = [Mon, Tue, Wed, Thu, Fri, Sat, Sun]

def weighted_choice(choices):
    total = sum(w for c, w in choices)
    r = random.uniform(0, total)
    upto = 0
    for c, w in choices:
        if upto + w >= r:
            return c
        upto += w
    assert False, "Shouldn't get here"

def half_tail(min, sigma):
    while True:
        r = random.gauss(min, sigma)
        if (r >= min) and (r < min * 10.0): # Select only positive half of gaussian distribution (and remove outliers beyond 10x the mean)
            return r

def cyclic_noise(id,t):    # Provides a somewhat cyclic (not completely stochastic) noise of +/- 0.5
    t = float(t + hash(id)) * 2 * pi    # a cycle every second, until divided
    return (sin(t/(MINS*7.5)) + sin(t/(HOURS*3.5)) + sin(t/DAYS) + sin(t/(DAYS*3)) + sin(t/(DAYS*7)) + sin(t/(DAYS*30)) + sin(t/(DAYS*47))) / (7*2) 

class Disruptive(Device):
    site_count = 1
    odd_site = False
    def __init__(self, instance_name, time, engine, update_callback, context, params):
        super(Disruptive,self).__init__(instance_name, time, engine, update_callback, context, params)
        self.sensor_type = params["disruptive"].get("sensor_type", None)
        self.site_prefix = params["disruptive"].get("site_prefix", "Fridge ")
        if self.sensor_type is None:
            if not Disruptive.odd_site: # Alternate type
                self.sensor_type = "temperature"
            else:
                self.sensor_type = "proximity"
            # self.sensor_type = weighted_choice([("ccon",5), ("temperature",38), ("proximity",33), ("touch",2)])
        self.set_property("sensorType", self.sensor_type)
        self.set_property("site", self.site_prefix + str(Disruptive.site_count))

        if(self.sensor_type != "ccon"):
            engine.register_event_in(BATTERY_INTERVAL, self.tick_battery, self, self)
            if(params["disruptive"].get("send_network_status", False)):
                engine.register_event_in(NETWORK_INTERVAL, self.tick_network, self, self)
        if(self.sensor_type == "ccon"):
            engine.register_event_in(CELLULAR_INTERVAL, self.tick_cellular, self, self)
        if(self.sensor_type == "temperature"):
            self.set_temperature(INTERNAL_TEMP_C)
            engine.register_event_in(TEMPERATURE_INTERVAL, self.tick_temperature, self, self)
            self.having_cooling_failure = False
        if(self.sensor_type == "proximity"):
            self.set_property("objectPresent", "PRESENT")   # Door starts closed
            self.schedule_next_presence_event()

        Disruptive.odd_site = not Disruptive.odd_site
        if not Disruptive.odd_site:
            Disruptive.site_count += 1

    def comms_ok(self):
        return super(Disruptive,self).comms_ok()

    def external_event(self, event_name, arg):
        super(Disruptive,self).external_event(event_name, arg)
        pass

    def close(self):
        super(Disruptive,self).close()

    # Private methods
    def tick_battery(self, _):
        self.set_property("eventType", "batteryPercentage", always_send=True)
        self.set_property("batteryPercentage", 100, always_send=True)
        self.engine.register_event_in(BATTERY_INTERVAL, self.tick_battery, self, self)

    def tick_network(self, _):
        self.set_property("eventType", "networkStatus", always_send=True)
        self.set_property("signalStrengthSensor", 100, always_send=True)
        self.engine.register_event_in(NETWORK_INTERVAL, self.tick_network, self, self)

    def tick_cellular(self, _):
        self.set_property("eventType", "cellularStatus", always_send=True)
        self.set_property("signalStrengthCellular", 100, always_send=True)
        self.engine.register_event_in(CELLULAR_INTERVAL, self.tick_cellular, self, self)

    def tick_temperature(self, _):
        # Check for cooling failure
        if COOLING_FAILURE_POSSIBLE:
            if not self.having_cooling_failure:
                chance_of_cooling_failure = float(TEMPERATURE_INTERVAL) / COOLING_FAILURE_MTBF
                if random.random() < chance_of_cooling_failure:
                    logging.info("Cooling failure on device "+str(self.get_property("$id")))
                    self.having_cooling_failure = True
            else:
                chance_of_failure_ending = TEMPERATURE_INTERVAL / COOLING_FAILURE_AV_TIME_TO_FIX
                if random.random() < chance_of_failure_ending:
                    logging.info("Cooling failure fixed on device "+str(self.get_property("$id")))

        # Check for door open
        peer = self.get_peer()
        door_open = peer.get_property("objectPresent") == "NOT_PRESENT"
        temp = self.get_temperature()

        if door_open or self.having_cooling_failure:
            temp = temp * (1.0 - TEMP_COUPLING) + (EXTERNAL_TEMP_C * TEMP_COUPLING)
        else:
            temp = INTERNAL_TEMP_C + cyclic_noise(self.get_property("$id"), self.engine.get_now()) * 2.0

        self.set_temperature(temp)
        self.engine.register_event_in(TEMPERATURE_INTERVAL, self.tick_temperature, self, self)

    def tick_presence(self, _):
        if self.get_property("objectPresent")=="PRESENT":   # Door currently closed
            self.set_property("objectPresent", "NOT_PRESENT")
        else:
            self.set_property("objectPresent", "PRESENT")
        self.schedule_next_presence_event()

    def schedule_next_presence_event(self):
        if self.get_property("objectPresent") == "PRESENT": # Door just closed, so consider when it should next open
            # Work-out when door should next open by exploring each hour (from current hour), with chance of door opening in that hour being set according to lookup table. If not open then keep moving forwards.
            t = time.gmtime(self.engine.get_now())   # Should be localised using lat/lon if available
            weekday = t.tm_wday  # Monday is 0
            hour = t.tm_hour + t.tm_min/60.0
            delta_hours = 0.0
            while True:
                # Explore next time period
                delta_hours += 1.0/AV_DOOR_OPENS_PER_HOUR
                hour += 1.0/AV_DOOR_OPENS_PER_HOUR
                if hour >= 24:
                    hour -= 24
                    weekday = (weekday + 1) % 7
                # logging.info("tick_presence considering weekday="+str(weekday)+" hour="+str(hour))
                chance_of_opening = normal_office_hours[weekday][int(hour)]/9.0  # Rescale 0..9 to 0..1
                if random.random() <= chance_of_opening:
                    break
            self.engine.register_event_in(delta_hours*60*60, self.tick_presence, self, self)
        else:   # Door just opened, so consider when it should next close
            if random.random() < 1.0/CHANCE_OF_DOOR_LEFT_OPEN:
                delay = half_tail(1 * HOURS, 24 * HOURS)
                logging.info("Door will be left open for "+str(delay)+"s on "+str(self.get_property("$id")))
            else:
                delay = half_tail(AV_DOOR_OPEN_MIN_TIME_S, AV_DOOR_OPEN_SIGMA_S)
            self.engine.register_event_in(delay, self.tick_presence, self, self)

    def get_peer(self):
        site = self.get_property("site", None)
        if site is None:
            return None
        me = self.get_property("$id")
        peer = None
        peers = device_factory.get_devices_by_property("site", site)    # All devices at this site
        for p in peers:
            if p.get_property("$id") != me:
                peer = p
        return peer

    def set_temperature(self, temperature):  # Temperature stored internally to higher precision than reported (so we can do e.g. asymptotes) 
        self.temperature = temperature
        self.set_property("eventType", "temperature", always_send=True)
        temp = int(temperature * 10) / 10.0
        self.set_property("temperature", temp, always_send=True)

    def get_temperature(self):
        return self.temperature

