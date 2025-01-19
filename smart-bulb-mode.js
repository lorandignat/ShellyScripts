let authToken = "Bearer TOKEN";
let serverUrl = "http://0.0.0.0:8123/api/states/light.test_bulb_light"

var currentMode = "momentary";

function testConnection(callback) {
   Shelly.call("HTTP.Request", { method: "GET", url: serverUrl, 
                                 headers: { "content-type" : "application/json", "authorization" : authToken }},
               function(resp) {
                 if (resp && resp.code === 200 && resp.body && JSON.parse(resp.body).state !== "unavailable") {
                   callback(true)
                 } else {
                   callback(false)
                 }
               });
}

function runIfSwitchOn(switchId, func) { 
  Shelly.call("Switch.GetStatus", { "id": switchId }, 
              function(status) { 
                if (status.output === true) {
                  func();
                } 
              });
}

function changeConnectionType(switchId) {
  runIfSwitchOn(switchId, function() {
    testConnection(function(result) {
      if (result === true) {
        print("-> Connection available")
        mode = "detached";
      } else {
        mode = "momentary";
      }
      if (currentMode !== mode) {
        runIfSwitchOn(switchId, function() {
          print("-> Switching mode")
          currentMode = mode;
          Shelly.call("Switch.SetConfig", { id: switchId, config: { in_mode: mode } }); 
        });
      }
    });
  });
}

function changeConnectionTypeForSwitchOne() {
  changeConnectionType(0)
}

print("-> Initializing Shelly")

Shelly.call("Switch.SetConfig", { id: 0, config: { in_mode: currentMode, initial_state: "off" } });
