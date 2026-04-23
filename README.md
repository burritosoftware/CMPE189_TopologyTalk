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

6. Install ryu-manager and test installation (e.g. on a mininet topology)
This assumes you have some mininet topology instantiated for the ryu-manager to connect to
```bash
cd ryu
pip install .
ryu-manager --observe-links network_logic/test.py ryu.app.rest_topology
```
