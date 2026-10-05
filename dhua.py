import requests
from requests.auth import HTTPDigestAuth

DVR_IP = "192.168.22.5"
USER = "Rsudjbg"
PASS = "Simrs1038"

url = f"http://{DVR_IP}/cgi-bin/configManager.cgi"
params = {"action": "getConfig", "name": "ChannelTitle"}

resp = requests.get(url, params=params, auth=HTTPDigestAuth(USER, PASS))
resp.raise_for_status()

for line in resp.text.splitlines():
    if line.startswith("table.ChannelTitle[") and ".Name=" in line:
        idx = line.split("[")[1].split("]")[0]
        name = line.split(".Name=", 1)[1]
        print(f"Channel {int(idx) + 1}: {name}")