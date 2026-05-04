#!/bin/bash
# Based on README.md instructions but including QoS and Conf Switch for tool support
ryu-manager --observe-links \
    ryu/ryu/app/rest_topology.py \
    ryu/ryu/app/rest_qos.py \
    ryu/ryu/app/rest_conf_switch.py \
    ryu/ryu/app/ofctl_rest.py
