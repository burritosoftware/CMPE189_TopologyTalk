#!/bin/bash
# Based on README.md instructions but including QoS and Conf Switch for tool support
ryu-manager \
  --ofp-tcp-listen-port 6633 \
  --observe-links \
  ryu/ryu/app/qos_simple_switch_13.py \
  ryu/ryu/app/rest_topology.py \
  ryu/ryu/app/rest_conf_switch.py \
  ryu/ryu/app/ofctl_rest.py \
  ryu/ryu/app/rest_qos.py
