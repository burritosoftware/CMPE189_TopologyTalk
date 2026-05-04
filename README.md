# CMPE189_TopologyTalk
Texting-Driven SDN Quality of Service Control (with Poke/MCP)

## Requirements
1. Python 3.12+ or later
2. A Ryu-based OpenFlow network
3. A free Poke account and compatible texting device

## Installation Instructions
1. Create or login to a Poke account at [Poke.com](https://poke.com)
2. Clone this repository.
```bash
git clone https://github.com/burritosoftware/CMPE189_TopologyTalk.git
```
3. Install dependencies (venv recommended).
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
4. Copy .env.example to .env and configure as necessary.
```bash
cp .env.example .env
```
5. Run the server.
```
python my_server.py
```
You will be asked to login to Poke in the browser if it's your first time.

6. In another terminal, install ryu-manager and run the controller. If you already have a working ryu installation, you may simply run the script:
```bash
cd ryu
python -m venv .venv
source .venv/bin/activate
pip install .
cd ..
./run_ryu.sh
```

7. In yet another terminal, run the test topology for the controller to connect to.
An example topology for testing and demoing is provided. For example, if running the controller on the same machine on the default port, set "ip=127.0.0.1,port=6633". E.g.:
```bash
sudo mn --topo linear,3 --mac \
  --controller=remote,ip=127.0.0.1,port=6633 \
  --switch ovsk,protocols=OpenFlow13
```
Note: Ensure the controller is connected by either checking the controller's log for some confirmation, e.g:
[QoS][INFO] dpid=0000000000000001: Join qos switch.

8. Done! Simply talk to Poke and manage and query a network topology with natural language!
