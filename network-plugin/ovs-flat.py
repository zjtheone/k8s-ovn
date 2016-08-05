#!/usr/bin/python
import argparse
import ast
import atexit
import getpass
import json
import os
import re
import requests
import shlex
import subprocess
import sys
import time
import uuid

from docker import Client

OVN_REMOTE = ""
OVN_BRIDGE = "ovs-br0"
OVN_SUBNET = "192.168.98.0/24"
OVN_MASK   = "24"
OVN_VLAN   = "98"
OVN_GATEWAY= "192.168.98.254"


def call_popen(cmd):
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    output = child.communicate()
    if child.returncode:
        raise RuntimeError("Fatal error executing %s" % (cmd))
    if len(output) == 0 or output[0] == None:
        output = ""
    else:
        output = output[0].strip()
    return output


def call_prog(prog, args_list):
    cmd = [prog, "--timeout=5", "-vconsole:off"] + args_list
    return call_popen(cmd)


def ovs_vsctl(args):
    return call_prog("ovs-vsctl", shlex.split(args))


def ovn_nbctl(args):
    args_list = shlex.split(args)
    database_option = "%s=%s" % ("--db", OVN_REMOTE)
    args_list.insert(0, database_option)
    return call_prog("ovn-nbctl", args_list)


def plugin_init(args):
    pass


def get_annotations(pod_name, namespace):
    api_server = ovs_vsctl("--if-exists get open_vswitch . "
                           "external-ids:api_server").strip('"')
    if not api_server:
        return None

    url = "http://%s/api/v1/pods" % (api_server)
    response = requests.get("http://0.0.0.0:8080/api/v1/pods")
    if response:
        pods = response.json()['items']
    else:
        return None

    for pod in pods:
        if (pod['metadata']['namespace'] == namespace and
           pod['metadata']['name'] == pod_name):
            annotations = pod['metadata'].get('annotations', "")
            if annotations:
                return annotations
            else:
                return None


def associate_security_group(lport_id, security_group_id):
    pass


def get_ovn_remote():
    try:
        global OVN_REMOTE
        OVN_REMOTE = ovs_vsctl("get Open_vSwitch . "
                               "external_ids:ovn-remote").strip('"')
    except Exception as e:
        error = "failed to fetch ovn-remote (%s)" % (str(e))


def _plugin_setup(ns,pod_name,container_id):

    client = Client(base_url='unix://var/run/docker.sock')
    try:
        inspect = client.inspect_container(container_id)
        pid = inspect["State"]["Pid"]
        ip_address = inspect["NetworkSettings"]["IPAddress"]
        netmask = inspect["NetworkSettings"]["IPPrefixLen"]
        mac = inspect["NetworkSettings"]["MacAddress"]
        gateway_ip = inspect["NetworkSettings"]["Gateway"]
    except Exception as e:
        error = "failed to get container pid and ip address (%s)" % (str(e))
        sys.exit(error)

    if not pid:
        sys.exit("failed to fetch the pid")

    netns_dir = "/var/run/netns"
    if not os.path.isdir(netns_dir):
        command = "mkdir -p %s" % netns_dir
        try:
            call_popen(shlex.split(command))
        except Exception as e:
            error = "failed to create the netns dir"
            sys.exit(error)


    netns_dst = "/var/run/netns/%s" % (pid)
    if not os.path.isfile(netns_dst):
        netns_src = "/proc/%s/ns/net" % (pid)
        command = "ln -s %s %s" % (netns_src, netns_dst)

        try:
            call_popen(shlex.split(command))
        except Exception as e:
            error = "failed to create the netns link"
            sys.exit(error)

    # Delete the existing veth pair
    command = "ip netns exec %s ip link del eth0" % (pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "failed to delete the default veth pair"
        sys.stderr.write(error)

    veth_outside = container_id[0:15]
    veth_inside = container_id[0:13] + "_c"
    command = "ip link add %s type veth peer name %s" \
              % (veth_outside, veth_inside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to create veth pair (%s)" % (str(e))
        sys.exit(error)

    # Up the outer interface
    command = "ip link set %s up" % veth_outside
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to admin up veth_outside (%s)" % (str(e))
        sys.exit(error)

    # Add veth_outside into OVN_BRIDGE
    ovs_vsctl("add-port %s %s tag=%s" % (OVN_BRIDGE,veth_outside, OVN_VLAN))
 

    # Move the inner veth inside the container
    command = "ip link set %s netns %s" % (veth_inside, pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to move veth inside (%s)" % (str(e))
        sys.exit(error)

    # Change the name of veth_inside to eth0
    command = "ip netns exec %s ip link set dev %s name eth0" \
              % (pid, veth_inside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to change name to eth0 (%s)" % (str(e))
        sys.exit(error)

    # Up the inner interface
    command = "ip netns exec %s ip link set eth0 up" % (pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to admin up veth_inside (%s)" % (str(e))
        sys.exit(error)

    # Set the mtu to handle tunnels
    #command = "ip netns exec %s ip link set dev eth0 mtu %s" \
    #          % (pid, 1450)
    #try:
    #    call_popen(shlex.split(command))
    #except Exception as e:
    #    error = "Failed to set mtu (%s)" % (str(e))
    #    sys.exit(error)

    # Set the ip address
    netmask = OVN_MASK
    command = "ip netns exec %s ip addr add %s/%s dev eth0" \
              % (pid, ip_address, netmask)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set ip address (%s)" % (str(e))
        sys.exit(error)

    # Set the mac address
    command = "ip netns exec %s ip link set dev eth0 address %s" % (pid, mac)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set mac address (%s)" % (str(e))
        sys.exit(error)

    # Set the gateway
    #command = "ip netns exec %s ip route add %s via %s" \
    #          % (pid, OVN_SUBNET, ip_address)
    #try:
    #    call_popen(shlex.split(command))
    #except Exception as e:
    #    error = "Failed to set gateway (%s)" % (str(e))
    #    sys.exit(error)


    # Set the gateway
    gateway_ip = OVN_GATEWAY 
    command = "ip netns exec %s ip route add default via %s" \
              % (pid, gateway_ip)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set gateway (%s)" % (str(e))
        sys.exit(error)

   

def plugin_setup(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]
    _plugin_setup(ns,pod_name,container_id)


def plugin_status(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]

    veth_outside = container_id[0:15]
    ip_address = ovs_vsctl("--if-exists get interface %s "
                           "external_ids:ip_address"
                           % (veth_outside)).strip('"')
    if ip_address:
        style = {"ip": ip_address}
        print json.dumps(style)


def disassociate_security_group(lport_id):
    pass


def plugin_teardown(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]

    get_ovn_remote()

    veth_outside = container_id[0:15]
    command = "ip link delete %s" % (veth_outside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to delete veth_outside (%s)" % (str(e))
        sys.stderr.write(error)

    annotations = get_annotations(ns, pod_name)
    if annotations:
        security_group = annotations.get("security-group", "")
        if security_group:
            disassociate_security_group(container_id)

    try:
        ovs_vsctl("del-port %s" % (veth_outside))
    except Exception as e:
        error = "failed to delete OVS port (%s)" % (veth_outside)
        sys.stderr.write(error)


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(title='Subcommands',
                                       dest='command_name')

    # Parser for sub-command init
    parser_plugin_init = subparsers.add_parser('init', help="kubectl init")
    parser_plugin_init.set_defaults(func=plugin_init)

    # Parser for sub-command setup
    parser_plugin_setup = subparsers.add_parser('setup',
                                                help="setup pod networking")
    parser_plugin_setup.add_argument('k8_args', nargs=3,
                                     help='arguments passed by kubectl')
    parser_plugin_setup.set_defaults(func=plugin_setup)

    # Parser for sub-command status
    parser_plugin_status = subparsers.add_parser('status',
                                                 help="pod status")
    parser_plugin_status.add_argument('k8_args', nargs=3,
                                      help='arguments passed by kubectl')
    parser_plugin_status.set_defaults(func=plugin_status)

    # Parser for sub-command teardown
    parser_plugin_teardown = subparsers.add_parser('teardown',
                                                   help="pod teardown")
    parser_plugin_teardown.add_argument('k8_args', nargs=3,
                                        help='arguments passed by kubectl')
    parser_plugin_teardown.set_defaults(func=plugin_teardown)

    args = parser.parse_args()
    args.func(args)

class Myargs:
    def __init__(selfk,ns,podname,container_id):
        k8_args = [ns,podname,container_id]


if __name__ == '__main__':
    main()
    #import pdb
    #pdb.set_trace()
    #_plugin_setup('default','busybox2',"41cb0b1e87fdf0162f4f97a34b861a2813cfdeb662726b44f5b490f59de4461b")
