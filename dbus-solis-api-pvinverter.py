#!/usr/bin/env python
# vim: ts=2 sw=2 et

# import normal packages
import platform 
import logging
import logging.handlers
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file
import base64
import datetime
from datetime import datetime, timezone
import hashlib
import hmac
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request
import traceback
import json
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

def getConfig():
  config = configparser.ConfigParser()
  config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
  return config

def getPosition():
  value = getConfig()['DEFAULT']['Position']
  
  if not value: 
      value = 0
  
  return int(value)

endpoint_inverter_list = "/v1/api/inverterList"
endpoint_inverter_detail = "/v1/api/inverterDetail"

def executeSolisApiRequest(endpoint, data, headers, retries) -> str:
    """execute request and handle errors"""
    url = getConfig()['DEFAULT']['Url'] + endpoint
    api_retries_timeout_s = int('1')
    if data != "":
        post_data = data.encode("utf-8")
        request = Request(url, data=post_data, headers=headers)
    else:
        request = Request(url)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            body_content = body.decode("utf-8")
            logging.debug("Decoded content: %s", body_content)
            return body_content
    except HTTPError as error:
        error_string = str(error.status) + ": " + error.reason

        if retries > 0:
            logging.warning(url + " -> " + error_string + " | retries left: " + retries )
            time.sleep(api_retries_timeout_s)
            executeSolisApiRequest(url, data, headers, retries - 1)
    except URLError as error:
        error_string = str(error.reason)
    except TimeoutError:
        error_string = "Request or socket timed out"
    except Exception as ex:  # pylint: disable=broad-except
        error_string = "urlopen exception: " + str(ex)
        traceback.print_exc()

    logging.error(url + " -> " + error_string)  # pylint: disable=used-before-assignment
    time.sleep(5)  # retry after 1 minute
    return "ERROR"

def getSolisCloudData(endpoint, data) -> str:
      """get solis cloud data"""
      
      apiKey = getConfig()['DEFAULT']['ApiKey']
      apiSecret = getConfig()['DEFAULT']['ApiSecret'].encode("utf-8")
      md5 = base64.b64encode(hashlib.md5(data.encode("utf-8")).digest()).decode("utf-8")
      while True:
          now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
          encrypt_str = (
                  'POST' + "\n"
                  + md5 + "\n"
                  + 'application/json' + "\n"
                  + now + "\n"
                  + endpoint
          )
          hmac_obj = hmac.new(
              apiSecret,
              msg=encrypt_str.encode("utf-8"),
              digestmod=hashlib.sha1,
          )
          authorization = (
                  "API "
                  + apiKey
                  + ":"
                  + base64.b64encode(hmac_obj.digest()).decode("utf-8")
          )
          headers = {
              "Content-MD5": md5,
              "Content-Type": 'application/json',
              "Date": now,
              "Authorization": authorization,
          }
          data_content = executeSolisApiRequest(endpoint, data, headers, retries=3)
          # logging.debug(url + url_part + " -> " + prettify_json(data_content))
          if data_content != "ERROR":
              return data_content
            
def get_inverter_list_body(
        inverter_id_val,
        inverter_sn_val,
        time_category='',
        time_string=''
) -> str:
    if time_category == "":
        body = '{"id":"' + inverter_id_val + '","sn":"' + inverter_sn_val + '"}'
    else:
        body = '{"id":"' + inverter_id_val + \
                '","sn":"' + inverter_sn_val + \
                '","' + time_category + \
                '":"' + time_string + '"}'
    logging.debug("body: %s", body)
    return body

def getSolisPvInverterDetails():
    apiKey = getConfig()['DEFAULT']['ApiKey']
    data_content = getSolisCloudData(endpoint_inverter_list, '{"userid":"' + apiKey + '"}')
    data_json = json.loads(data_content)["data"]["inverterStatusVo"]
    entries = data_json["all"]-1
    device_id = 0
    if device_id < 0:
        logging.error("'SOLIS_CLOUD_API_INVERTER_ID' has to be greater or equal to 0 " + \
                        "and lower than %s.", str(entries))
    if device_id > entries:
        logging.error("Your 'SOLIS_CLOUD_API_INVERTER_ID' (%s" + \
                        ") is larger than or equal to the available number of inverters (" + \
                        "%s). Please select a value between '0' and '%s'.", str(device_id),
                        str(entries), str(entries - 1))
    data_json = json.loads(data_content)["data"]["page"]["records"]
    station_info = data_json[device_id]
    inverter_id = station_info["id"]
    inverter_sn = station_info["sn"]

    inverter_detail_body = get_inverter_list_body(inverter_id, inverter_sn)
    content = getSolisCloudData(endpoint_inverter_detail, inverter_detail_body)
    return json.loads(content)["data"]

class DbusSolisApiPvInverterService:
  def __init__(self, paths):
    inverterDetails = getSolisPvInverterDetails()
    inverterModel = inverterDetails["machine"]
    inverterFirmwareVersion = inverterDetails["version"]
    serialNumber = inverterDetails["sn"]
    
    config = getConfig()
    servicename = 'com.victronenergy.pvinverter'
    deviceinstance = int(config['DEFAULT']['DeviceInstance'])
    productName = "Solis PV Inverter - {}".format(inverterModel)
    customName = productName
    productid = 0xA144

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths
 
    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
 
    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', 'HTTP - Solis Api')
 
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', productid)
    self._dbusservice.add_path('/DeviceType', 345) # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
    self._dbusservice.add_path('/ProductName', productName)
    self._dbusservice.add_path('/CustomName', customName)
    self._dbusservice.add_path('/Latency', None)
    self._dbusservice.add_path('/FirmwareVersion', inverterFirmwareVersion)
    self._dbusservice.add_path('/HardwareVersion', inverterModel)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/Role', 'pvinverter')
    self._dbusservice.add_path('/Position', getPosition()) # normaly only needed for pvinverter
    self._dbusservice.add_path('/Serial', serialNumber)
    self._dbusservice.add_path('/UpdateIndex', 0)
 
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)
 
    # last update
    self._lastUpdate = 0
 
    # add _update function 'timer'
    updateEvery = 1000 * 60
    gobject.timeout_add(updateEvery, self.update) # pause 500ms before the next request

  def update(self):   
    try:
      inverter_detail = getSolisPvInverterDetails()
      
      power = inverter_detail["pac"] * 1000
      logging.info(power)
      voltage = inverter_detail["uAc1"]
      logging.info(voltage)
      current = power/voltage

      #send data to DBus
      self._dbusservice['/Ac/Power'] = power
      self._dbusservice['/Ac/L1/Voltage'] = voltage
      # self._dbusservice['/Ac/L2/Voltage'] = 0
      # self._dbusservice['/Ac/L3/Voltage'] = 0
      self._dbusservice['/Ac/L1/Current'] = current
      # self._dbusservice['/Ac/L2/Current'] = 0
      # self._dbusservice['/Ac/L3/Current'] = 0
      self._dbusservice['/Ac/L1/Power'] = power
      # self._dbusservice['/Ac/L2/Power'] = 0
      # self._dbusservice['/Ac/L3/Power'] = 0
      # self._dbusservice['/Ac/L1/Energy/Forward'] = 0
      # self._dbusservice['/Ac/L2/Energy/Forward'] = 0
      # self._dbusservice['/Ac/L3/Energy/Forward'] = 0
      # self._dbusservice['/Ac/L1/Energy/Reverse'] = 0
      # self._dbusservice['/Ac/L2/Energy/Reverse'] = 0
      # self._dbusservice['/Ac/L3/Energy/Reverse'] = 0
      
      # Old version
      #self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/L1/Energy/Forward'] + self._dbusservice['/Ac/L2/Energy/Forward'] + self._dbusservice['/Ac/L3/Energy/Forward']
      #self._dbusservice['/Ac/Energy/Reverse'] = self._dbusservice['/Ac/L1/Energy/Reverse'] + self._dbusservice['/Ac/L2/Energy/Reverse'] + self._dbusservice['/Ac/L3/Energy/Reverse'] 
      
      # New Version - from xris99
      #Calc = 60min * 60 sec / 0.500 (refresh interval of 500ms) * 1000
      # updateTime = 1000 * 60 / 500
      # if (self._dbusservice['/Ac/Power'] > 0):
      #      self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/Energy/Forward'] + (self._dbusservice['/Ac/Power']/(60*60/updateTime*1000))            
      # if (self._dbusservice['/Ac/Power'] < 0):
      #      self._dbusservice['/Ac/Energy/Reverse'] = self._dbusservice['/Ac/Energy/Reverse'] + (self._dbusservice['/Ac/Power']*-1/(60*60/updateTime*1000))

      
      #logging
      # logging.debug("House Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
      # logging.debug("House Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
      # logging.debug("House Reverse (/Ac/Energy/Revers): %s" % (self._dbusservice['/Ac/Energy/Reverse']))
      # logging.debug("---");
      
      # increment UpdateIndex - to show that new data is available an wrap
      self._dbusservice['/UpdateIndex'] = (self._dbusservice['/UpdateIndex'] + 1 ) % 256

      #update lastupdate vars
      self._lastUpdate = time.time()
    except (ValueError, requests.exceptions.ConnectionError, requests.exceptions.Timeout, ConnectionError) as e:
       logging.critical('Error getting data from Shelly - check network or Shelly status. Setting power values to 0. Details: %s', e, exc_info=e)       
       self._dbusservice['/Ac/L1/Power'] = 0                                       
      #  self._dbusservice['/Ac/L2/Power'] = 0                                       
      #  self._dbusservice['/Ac/L3/Power'] = 0
       self._dbusservice['/Ac/Power'] = 0
       self._dbusservice['/UpdateIndex'] = (self._dbusservice['/UpdateIndex'] + 1 ) % 256        
    except Exception as e:
       logging.critical('Error at %s', 'update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change




def getLogLevel():
  config = configparser.ConfigParser()
  config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
  logLevelString = config['DEFAULT']['LogLevel']
  
  if logLevelString:
    level = logging.getLevelName(logLevelString)
  else:
    level = logging.INFO
    
  return level


def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=getLogLevel(),
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
 
  try:
      logging.info("Start");
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + ' kWh')
      _a = lambda p, v: (str(round(v, 1)) + ' A')
      _w = lambda p, v: (str(round(v, 1)) + ' W')
      _v = lambda p, v: (str(round(v, 1)) + ' V')   
     
      #start our main-service
      pvac_output = DbusSolisApiPvInverterService(
        paths={
          # '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh}, # energy bought from the grid
          # '/Ac/Energy/Reverse': {'initial': 0, 'textformat': _kwh}, # energy sold to the grid
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          
          '/Ac/Current': {'initial': 0, 'textformat': _a},
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          
          '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
          # '/Ac/L2/Voltage': {'initial': 0, 'textformat': _v},
          # '/Ac/L3/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
          # '/Ac/L2/Current': {'initial': 0, 'textformat': _a},
          # '/Ac/L3/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          # '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          # '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          # '/Ac/L1/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          # '/Ac/L2/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          # '/Ac/L3/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          # '/Ac/L1/Energy/Reverse': {'initial': 0, 'textformat': _kwh},
          # '/Ac/L2/Energy/Reverse': {'initial': 0, 'textformat': _kwh},
          # '/Ac/L3/Energy/Reverse': {'initial': 0, 'textformat': _kwh},
        })
     
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except (ValueError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
    logging.critical('Error in main type %s', str(e))
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
