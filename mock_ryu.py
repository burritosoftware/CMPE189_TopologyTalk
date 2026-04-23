from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# Mock data
topology = {
    "switches": [{"dpid": "0000000000000001"}],
    "links": []
}

qos_queues = {}
qos_rules = {}

@app.route('/v1.0/topology/switches', methods=['GET'])
def get_switches():
    return jsonify(topology["switches"])

@app.route('/v1.0/topology/links', methods=['GET'])
def get_links():
    return jsonify(topology["links"])

@app.route('/qos/queue/<switch_id>', methods=['GET', 'POST', 'DELETE'])
def manage_queues(switch_id):
    if request.method == 'GET':
        return jsonify(qos_queues.get(switch_id, {}))
    elif request.method == 'POST':
        data = request.json
        qos_queues[switch_id] = data
        return jsonify({"command_result": [{"result": "success", "details": "Configured queues"}]})
    elif request.method == 'DELETE':
        qos_queues.pop(switch_id, None)
        return jsonify({"command_result": [{"result": "success", "details": "Deleted queues"}]})

@app.route('/qos/rules/<switch_id>', methods=['GET'])
def get_rules(switch_id):
    return jsonify(qos_rules.get(switch_id, []))

@app.route('/qos/<switch_id>', methods=['POST'])
def add_rule(switch_id):
    data = request.json
    if switch_id not in qos_rules:
        qos_rules[switch_id] = []
    qos_rules[switch_id].append(data)
    return jsonify([{"result": "success", "details": "Added rule"}])

@app.route('/qos/queue/status/<switch_id>', methods=['GET'])
def get_qos_status(switch_id):
    return jsonify({switch_id: []})

if __name__ == "__main__":
    print("Starting Mock Ryu Controller on port 8080...")
    app.run(port=8080)
