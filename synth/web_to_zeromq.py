"""
For this module to work, the following SSL certificate files must be placed in ../synth_accounts/:
    ssl.crt 
    ssl.key

Flask occasionally just stops responding to web requests (like every day or so) - no idea why. So we rely on an external service (e.g. Pingdom or UptimeRobot) to ping us regularly and then, knowing that that is happening, if we don't recive any messages then we know to restart the server.

GET /?<magickey>
----------------
Return a basic page listing all running Synth processes and free memory.
For security this must be accompanied by a magic key matching the "web_check_key" property in the file ../synth_accounts/default.json

GET /spawn?devicepilot_key=XXX&devicepilot_api=staging
------------------------------------------------------
Spawn a new instance of Synth, with these two specific parameters set. The UserDemo scenario is run.
The instance_name is set to be "devicepilot_key=XXX" since that is assumed to be unique.

GET /is_running?devicepilot_key=XXX&devicepilot_api=staging
-----------------------------------------------------------
Find whether a specific instance of Synth (identified by its key) is still running. Return a JSON struct with active=true/false.

GET /plots/<filename>
---------------------
Return a plot generated by the Expect device function

POST /event
-----------
This causes an inbound device event to be asynchronously generated, to a specific device on a specific Synth instance. 
The header of the web request must contain the following::

    Key : <web_key parameter> 
    Instancename : <instance_name parameter>

The body of the web request must contain a JSON set include the following elements::

    "deviceId" : "theid"
    "eventName" : "replace_battery" | "upgradeFirmware" | "factoryReset"
    "arg" : "0.7" - optional argument only relevant for some eventNames

If defining a Webhook Action in DevicePilot to create a Synth event, the device ID will be automatically filled-out if you define it as {device.$id},
resulting in an action specification which looks something like this::

    method: POST
    url: https://synthservice. com/event
    headers: { "Key":"mywebkey", "Instancename" : "OnProductionAccountUser" }
    body: { "deviceId" : "{device.$id}", "eventName" : "upgadeFirmware", "arg":"0.7"}
"""

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

# It seems generally accepted that API POSTs should take no more than 2 seconds, so we need
# to be pretty responsive. So we run Flask in Threaded mode, and protect child code for re-entrancy
# where necessary.
#
# We also run Flask in its own *Process*.
# WARNING: it appears that if you bind ZMQ to a socket on multiple processes
# then the second one fails *silently*! So don't call socket_send() from the parent
# process, or Flask's ZMQ sends will all fail silently.

from flask import Flask, request, abort
from flask_cors import CORS, cross_origin
import multiprocessing, subprocess, threading
import json, time, logging, sys, re, datetime
import zmq # pip install pyzmq-static

WEB_PORT = 80 # HTTPS. If < 1000 then this process must be run with elevated privileges
PING_TIMEOUT = 60*10 # We expect to get pinged every N seconds
ZEROMQ_PORT = 5556
ZEROMQ_BUFFER = 100000

CERT_DIRECTORY = "../synth_accounts/"

DEFAULTS_FILE = "../synth_accounts/default.json"

g_zeromq_socket = None # Global, protected via g_lock
g_lock = None
g_last_ping_time = multiprocessing.Value('d', time.time())

app = Flask(__name__)
CORS(app)   # Make Flask tell browser that cross-origin requests are OK

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

def create_socket():
    global g_zeromq_socket
    logging.info("Initialising ZeroMQ to publish to TCP port "+str(ZEROMQ_PORT)+ " with buffer size " + str(ZEROMQ_BUFFER))
    context = zmq.Context()
    g_zeromq_socket = context.socket(zmq.PUB)
    g_zeromq_socket.bind("tcp://*:%s" % ZEROMQ_PORT)
    g_zeromq_socket.set_hwm(ZEROMQ_BUFFER)

def socket_send(json_msg):
    global g_lock, g_zeromq_socket
    t = time.time()
    
    g_lock.acquire()
    try:
        if g_zeromq_socket == None:
            create_socket()
        g_zeromq_socket.send(json.dumps(json_msg))
    except:
        logging.error("ERROR in socket_send")
        logging.critical(traceback.format_exc())
        raise
    finally:
        g_lock.release()

    elapsed = time.time() - t
    if elapsed > 0.1:
        logging.error("****** SLOW POSTING *******")
        logging.error("Took "+str(elapsed)+"s to post event")
    
@app.route("/event", methods=['POST','GET'])
def event():
    """Accept an incoming event and route it to a Synth instance."""

    logging.info("Got web request to /event")
    h = {}
    for (key,value) in request.headers:
        h[key] = value
    packet = {
        "action" : "event",
        "headers" : h,
        "body" : request.get_json(force=True)
        }
    logging.info(str(packet))
    socket_send(packet)
    return "ok"

def getAndCheckKey(req):
    if not "devicepilot_key" in req.args:
        logging.error("Missing devicepilot_key argument")
        return None

    dpKey = req.args["devicepilot_key"]

    # Defend against injection attack
    if len(dpKey) != 32:
        logging.error("Bad key length")
        return None
    if re.search(r'[^a-z0-9]', dpKey):
        logging.error("Bad key characters")
        return None

    return dpKey

def getAndCheckApi(req):
    """Stop external users passing-in any old URL."""
    if not "devicepilot_api" in req.args:
        dpApi = "api"
    else:
        dpApi = req.args["devicepilot_api"]

    # Defend against injection attack
    if not dpApi in ["api","api-staging","api-development"]:
        dpApi = "api"

    return "https://"+dpApi+".devicepilot.com"
    
@app.route("/spawn", methods=['GET'])
def spawn():
    """Start a new Synth instance."""
    logging.info("Got web request to /spawn")
    dpKey = getAndCheckKey(request)
    if dpKey==None:
        abort(403)

    dpApi = getAndCheckApi(request)

    packet = { "action" : "spawn", "key" : dpKey, "api" : dpApi }
    socket_send(packet)
    logging.info("Sent packet "+str(packet))
    time.sleep(1)   # If client next immediately tests <is_running>, this will vastly increase chances of that working
    return "ok"

@app.route("/plots/<filename>", methods=['GET'])
def plots(filename):
    """Serve plots from special directory"""
    logging.info("Got web request to "+str(request.path)+" so filename is "+str(filename))

    if re.search(r'[^A-Za-z0-9.]', filename):
        for c in filename:
            logging.info(str(ord(c)))
        logging.info("Illegal .. in pathname")
        abort(400)

    try:
        f=open("../synth_logs/plots/"+filename).read()
    except:
        logging.warning("Can't open file")
        return ("Can't open that file")
    return f

@app.route("/is_running")
def isRunning():
    logging.info("Got web request to /is_running")
    dpKey = getAndCheckKey(request)
    if dpKey==None:
        abort(403)

    try:
        x = subprocess.check_output("ps uax | grep 'python' | grep 'devicepilot_key=" + dpKey + "' | grep -v grep", shell=True)
    except:
        return '{ "active" : false }'
    return '{ "active" : true }'

@app.route("/ping")
def ping():
    """We expect Pingdom to regularly ping this route to reset the heartbeat."""
    global g_last_ping_time
    logging.info("Saw request to /ping")
    g_last_ping_time.value = time.time()
    socket_send({"action": "ping"})   # Propagate pings into ZeroMQ for liveness logging throughout rest of system
    return "pong"

@app.route("/")
def whatIsRunning():
    logging.info("Got web request to /")

    try:
        magicKey=json.loads(open(DEFAULTS_FILE,"rt").read())["web_check_key"]
    except:
        logging.error("Unable to find web_check_key parameter in "+DEFAULTS_FILE)
        raise

    if magicKey not in request.args:
        logging.error("Incorrect or missing magic key in request")
        abort(403)
        
    try:
        x = subprocess.check_output("ps uax | grep 'python' | grep -v grep", shell=True)
        x += "<br>"
        x += subprocess.check_output("free -m", shell=True)
    except:
            return "Nothing"
    return "<pre>"+x.replace("\n","<br>")+"</pre>"

# DO NOT CALL socket_send() BELOW HERE, OR YOU WILL BORK IT

def start_web_server(restart):
    """Doing app.run() with "threaded=True" starts a new thread for each incoming request, improving crash resilience. However this then means that everything here (and everything it calls) has to be re-entrant. So don't do that.
       By default Flask serves to 127.0.0.1 which is local loopback (not externally-visible), so use 0.0.0.0 for externally-visible
       We run entire Flask server as a distinct process so we can terminate it if it fails (can't terminate threads in Python)"""

    global g_lock
    logging.info("Starting web server at "+datetime.datetime.now().ctime())
    g_lock = threading.Lock()
    args = {    "threaded":True,
                "host":"0.0.0.0",
                "port":WEB_PORT
            }
    logging.info("Starting Flask server with args : "+json.dumps(args))
    p = multiprocessing.Process(target=app.run, kwargs=args)
    p.daemon = True
    p.start()
    return p

if __name__ == "__main__":
    server = start_web_server(restart=False)
    while True:
        time.sleep(1)
        if time.time()-g_last_ping_time.value > PING_TIMEOUT:
            logging.critical("Web server not detecting pings - restarting")
            server.terminate()
            time.sleep(5)
            server = start_web_server(restart=True)
            time.sleep(60)
