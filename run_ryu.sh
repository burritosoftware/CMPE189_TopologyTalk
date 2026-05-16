#!/bin/bash
# Optimized ryu-manager execution with base simple_switch_13
ryu-manager \
  --ofp-tcp-listen-port 6633 \
  --observe-links \
  ryu/ryu/app/simple_switch_13.py \
  ryu/ryu/app/ofctl_rest.py \
  ryu/ryu/app/rest_topology.py
