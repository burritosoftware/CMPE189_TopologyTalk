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

7. Connect the controller to the topology you wish to interact with.
An example topology in mininet for testing and demoing is provided. Run it with:
```bash
sudo mn --custom diamond_topo.py --topo diamond --mac \
  --controller=remote,ip=127.0.0.1,port=6633 \
  --switch ovsk,protocols=OpenFlow13 \
  --link tc
```
<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/4b8fd073-5a57-4c88-bacf-3ac704b6b869" />

Note: Ensure the controller is connected to the topology by checking the controller's log for some confirmation, e.g:

[QoS][INFO] dpid=0000000000000001: Join qos switch.

In addition, ensure the slow path is the path initilized. We can exploit link learning for automatic flow rule installation to do it for us! In mininet, run the provided script:
```bash
mininet> source slow_path_init.mn
```

8. Done! Simply talk to Poke and manage and query and manage the QoS of a network topology with natural language!
