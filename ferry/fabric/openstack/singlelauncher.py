# Copyright 2014 OpenCore LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from heatclient import client as heat_client
from heatclient.exc import HTTPUnauthorized
from neutronclient.neutron import client as neutron_client
import json
import logging
import math
import os
import sys
import time
import yaml

class SingleLauncher(object):
    """
    Launches new Ferry containers on an OpenStack cluster.

    Unlike the multi-launcher, containers use a single pre-assigned
    network for all communication. This makes it suitable for OpenStack
    environments that only support a single network (i.e., HP Cloud). 
    """
    def __init__(self, controller, conf_file):
        self.docker_registry = None
        self.docker_user = None
        self.heat_server = None
        self.openstack_key = None

        self.apps = {}

        self.controller = controller
        self._init_open_stack(conf_file)

    def _init_open_stack(self, conf_file):
        with open(conf_file, 'r') as f:
            args = yaml.load(f)

            # First we need to know the deployment system
            # we are using. 
            provider = args['system']['provider']

            # Now get some basic OpenStack information
            params = args[provider]['params']
            self.default_dc = params['dc']
            self.default_zone = params['zone']

            # Some information regarding OpenStack
            # networking. Necessary for 
            servers = args[provider][self.default_dc]
            self.manage_network = servers['network']
            self.external_network = servers['extnet']

            # OpenStack API endpoints. 
            self.keystone_server = servers['keystone']
            self.neutron_server = servers['neutron']
            if 'HEAT_URL' in os.environ:
                self.heat_server = os.environ['HEAT_URL']
            else:
                self.heat_server = servers['heat']

            # This gives us information about the image to use
            # for the supplied provider. 
            deploy = args[provider]['deploy']
            self.default_image = deploy['image']
            self.ferry_volume = deploy['image-volume']
            self.default_personality = deploy['personality']
            self.ssh_key = deploy['ssh']
            self.ssh_user = deploy['ssh-user']

            # some OpenStack login credentials. 
            self.openstack_user = os.environ['OS_USERNAME']
            self.openstack_pass = os.environ['OS_PASSWORD']
            self.tenant_id = os.environ['OS_TENANT_ID']
            self.tenant_name = os.environ['OS_TENANT_NAME']
            self.auth_tok = os.environ['OS_TOKEN']

            # Initialize the OpenStack clients and also
            # download some networking information (subnet ID, 
            # cidr, gateway, etc.)
            self._init_openstack_clients()
            self._collect_subnet_info()

    def _init_openstack_clients(self):
        if 'HEAT_API_VERSION' in os.environ:
            heat_api_version = os.environ['HEAT_API_VERSION']
        else:
            heat_api_version = '1'
        kwargs = {
            'username' : self.openstack_user,
            'password' : self.openstack_pass,
            'include_pass' : True,
            'tenant_id': self.tenant_id,
            'tenant_name': self.tenant_name,
            'token': self.auth_tok,
            'auth_url' : self.keystone_server
        }
        self.heat = heat_client.Client(heat_api_version, 
                                       self.heat_server, 
                                       **kwargs)

        neutron_api_version = "2.0"
        kwargs['endpoint_url'] = self.neutron_server
        self.neutron = neutron_client.Client(neutron_api_version, 
                                             **kwargs)

    def _create_floating_ip(self, name, port):
        """
        Create and attach a floating IP to the supplied port. 
        """
        plan =  { name : { "Type": "OS::Neutron::FloatingIP",
                           "Properties": { "floating_network_id": self.external_network }},
                  name + "_assoc" : { "Type": "OS::Neutron::FloatingIPAssociation",
                                      "Properties": { "floatingip_id": { "Ref" : name },
                                                      "port_id": { "Ref" : port }}}}
        desc = { "type" : "OS::Neutron::FloatingIP" }
        return plan, desc

    def _create_security_group(self, group_name, ports):
        """
        Create and assign a security group to the supplied server. 
        """

        # Create the basic security group. 
        # This only includes SSH. We can later update the group
        # to include additional ports. 
        desc = { group_name : { "Type" : "OS::Neutron::SecurityGroup",
                                "Properties" : { "name" : group_name,
                                                 "description" : "Ferry firewall rules", 
                                                 "rules" : [ { "protocol" : "icmp",
                                                               "remote_ip_prefix": "0.0.0.0/0" },
                                                             { "protocol" : "tcp",
                                                               "remote_ip_prefix": "0.0.0.0/0",
                                                               "port_range_min" : 22,
                                                               "port_range_max" : 22 }]}}}
        # Additional ports for the security group. 
        for p in ports:
            min_port = p[0]
            max_port = p[1]
            desc[group_name]["Properties"]["rules"].append({ "protocol" : "tcp",
                                                             "port_range_min" : min_port,
                                                             "port_range_max" : max_port })
        return desc
        
    def _create_storage_volume(self, volume_name, server_name, size_gb):
        """
        Create and attach a storage volume to the supplied server. 
        """
        desc = { volume_name : { "Type" : "OS::Cinder::Volume",
                                 "Properties": { "size" : size_db,
                                                 "availability_zone": self.default_zone
                                 }},
                 volume_name + "_attachment" : { "Type" : "OS::Cinder::VolumeAttachment",
                                                 "Properties": { "volume_id" : { "Ref" : volume_name },
                                                                 "instance_uuid": { "Ref" : server_name },
                                                                 "mount_point": "/dev/vdc"
                                                             }}}
        return desc

    def _create_port(self, name, network, sec_group, ref=True):
        desc = { name : { "Type" : "OS::Neutron::Port",
                          "Properties" : { "name" : name,
                                           "security_groups" : [{ "Ref" : sec_group }]}}}
        if ref:
            desc[name]["Properties"]["network"] = { "Ref" : network }
        else:
            desc[name]["Properties"]["network"] = network 

        return desc

    def _create_server_init(self, instance_name, networks):
        """
        Create the server init process. These commands are run on the
        host after the host has booted up. 
        """

        user_data = {
            "Fn::Base64": {
              "Fn::Join": [
                "",
                  [
                    "#!/bin/bash -v\n",
                    "umount /mnt\n", 
                    "parted --script /dev/vdb mklabel gpt\n", 
                    "parted --script /dev/vdb mkpart primary xfs 0% 100%\n",
                    "mkfs.xfs /dev/vdb1\n", 
                    "mkdir /ferry/data\n",
                    "mkdir /ferry/keys\n",
                    "mount -o noatime /dev/vdb1 /ferry/data\n",
                    "export FERRY_SCRATCH=/ferry/data\n", 
                    "export FERRY_DIR=/ferry/master\n",
                    "export HOME=/root\n",
                    "export USER=root\n",
                    "mkdir /home/ferry/.ssh\n",
                    "cp /home/ubuntu/.ssh/authorized_keys /home/ferry/.ssh/\n",
                    "chown -R ferry:ferry /home/ferry/.ssh\n",
                    "dhclient eth1\n",
                    "ferry server\n"
                  ]
              ]
          }}
        return user_data

    def _create_volume_attachment(self, iface, instance, volume_id):
        plan = { iface: { "Type": "OS::Cinder::VolumeAttachment",
                          "Properties": { "instance_uuid": { "Ref" : instance },
                                          "mountpoint": "/dev/vdc", 
                                          "volume_id": volume_id}}}
        desc = { "type" : "OS::Cinder::VolumeAttachment" }
        return plan, desc

    def _create_instance(self, name, image, size, manage_network, sec_group):
        """
        Create a new instance
        """
        plan = { name : { "Type" : "OS::Nova::Server",
                          "Properties" : { "name" : name, 
                                           "image" : image,
                                           "key_name" : self.ssh_key, 
                                           "flavor" : size,
                                           "availability_zone" : self.default_zone, 
                                           "networks" : []}}} 
        desc = { name : { "type" : "OS::Nova::Server",
                          "ports" : [],
                          "volumes" : [] }}

        # Create a port for the manage network.
        port_descs = []
        port_name = "ferry-port-%s" % name
        port_descs.append(self._create_port(port_name, manage_network, sec_group, ref=False))
        plan[name]["Properties"]["networks"].append({ "port" : { "Ref" : port_name },
                                                      "network" : manage_network}) 
        desc[name]["ports"].append(port_name)
        desc[port_name] = { "type" : "OS::Neutron::Port",
                            "role" : "manage" }
                                                      
        # Combine all the port descriptions. 
        for d in port_descs:
            plan = dict(plan.items() + d.items())

        # Now add the user script.
        user_data = self._create_server_init(name, data_networks)
        plan[name]["Properties"]["user_data"] = user_data

        return plan, desc

    def _output_instance_info(self, info_name, server_name):
        desc = {info_name : { "Value" : { "Fn::GetAtt" : [server_name, "PrivateIp"]}}}
        return desc

    def _create_floatingip_plan(self, cluster_uuid, ifaces):
        """
        Assign floating IPs to the supplied interfaces/ports. 
        """
        plan = { "AWSTemplateFormatVersion" : "2010-09-09",
                 "Description" : "Ferry generated Heat plan",
                 "Resources" : {} }
        desc = {}
        for i in range(0, len(ifaces)):
            ip_name = "ferry-ip-%s-%d" % (cluster_uuid, i)
            ip_plan, desc[ip_name] = self._create_floating_ip(ip_name, ifaces[i])
            plan["Resources"] = dict(plan["Resources"].items() + ip_plan.items())

        return plan, desc

    def _create_security_plan(self, cluster_uuid, ports):
        """
        Update the security group. 
        """
        sec_group_name = "ferry-sec-%s" % cluster_uuid
        plan = { "AWSTemplateFormatVersion" : "2010-09-09",
                 "Description" : "Ferry generated Heat plan",
                 "Resources" : self._create_security_group(sec_group_name, ports) }
        desc = { sec_group_name : { "type" : "OS::Neutron::SecurityGroup" }}
        return plan, desc

    def _create_instance_plan(self, cluster_uuid, num_instances, image, size, sec_group_name, ctype): 
        plan = { "AWSTemplateFormatVersion" : "2010-09-09",
                 "Description" : "Ferry generated Heat plan",
                 "Resources" : {},
                 "Outputs" : {} }
        desc = {}

        for i in range(0, num_instances):
            # Create the actual instances. 
            instance_name = "ferry-instance-%s-%s-%d" % (cluster_uuid, ctype, i)
            instance_plan, instance_desc = self._create_instance(instance_name, image, size, self.manage_network, sec_group_name)
            plan["Resources"] = dict(plan["Resources"].items() + instance_plan.items())
            desc = dict(desc.items() + instance_desc.items())

            # # Attach the Ferry image volume to the instance. 
            # attach_name = "ferry-attach-%s-%s-%d" % (cluster_uuid, ctype, i)
            # vol_plan, vol_desc = self._create_volume_attachment(attach_name, instance_name, self.ferry_volume)
            # plan["Resources"] = dict(plan["Resources"].items() + vol_plan.items())
            # desc = dict(desc.items() + vol_desc.items())

        return plan, desc

    def _launch_heat_plan(self, stack_name, heat_plan, stack_desc):
        """
        Launch the cluster plan.  
        """
        logging.info("launching heat plan: " + str(heat_plan))
        
        # Instruct Heat to create the stack, and wait 
        # for it to complete. 
        resp = self.heat.stacks.create(stack_name=stack_name, template=heat_plan)
        if not self._wait_for_stack(resp["stack"]["id"]):
            logging.warning("Network stack %s CREATE_FAILED" % resp["stack"]["id"])
            return None

        # Now find the physical IDs of all the resources. 
        resources = self._collect_resources(resp["stack"]["id"])
        for r in resources:
            if r["logical_resource_id"] in stack_desc:
                stack_desc[r["logical_resource_id"]]["id"] = r["physical_resource_id"]

        # Record the Stack ID in the description so that
        # we can refer back to it later. 
        stack_desc[stack_name] = { "id" : resp["stack"]["id"],
                                   "type": "OS::Heat::Stack" }
        return stack_desc

    def _update_heat_plan(self, stack_id, stack_plan):
        """
        Update the cluster plan. 
        """
        self.heat.stacks.update(stack_id, template=stack_plan)

    def release_ip_plan(self, ips):
        plan = { "AWSTemplateFormatVersion" : "2010-09-09",
                 "Description" : "Ferry generated Heat plan",
                 "Resources" : {} }

        for i in ips:
            plan["Resources"] = {
                i["name"] : { "Type": "OS::Neutron::FloatingIPAssociation",
                              "Properties": {} }}

        return plan

    def _wait_for_stack(self, stack_id):
        """
        Wait for stack completion.
        """
        while(True):
            try:
                stack = self.heat.stacks.get(stack_id)
                if stack.status == "COMPLETE":
                    return True
                elif stack.status == "FAILED":
                    return False
                else:
                    time.sleep(2)
            except HTTPUnauthorized as e:
                logging.warning(e)

    def _collect_resources(self, stack_id):
        """
        Collect all the stack resources so that we can create
        additional plans and use IDs. 
        """
        resources = self.heat.resources.list(stack_id)
        descs = [r.to_dict() for r in resources]
        return descs

    def _collect_subnet_info(self):
        """
        Collect the data network subnet info (ID, CIDR, and gateway). 
        """
        subnets = self.neutron.list_subnets()
        for s in subnets['subnets']:
            logging.warning("SUBNET: " + str(s))

        self.subnet = { "id" : None,
                        "cidr" : None, 
                        "gateway" : None }

    def _collect_network_info(self, stack_desc):
        """
        Collect all the networking information. 
        """

        # First get the floating IP information. 
        ip_map = {}
        floatingips = self.neutron.list_floatingips()
        for f in floatingips['floatingips']:
            if f['fixed_ip_address']:
                ip_map[f['fixed_ip_address']] = f['floating_ip_address']

        # Now fill in the various networking information, including
        # subnet, IP address, and floating address. We should also
        # probably collect MAC addresseses..
        ports = self.neutron.list_ports()
        for p in ports['ports']:
            if p['name'] != "" and p['name'] in stack_desc:
                port_desc = stack_desc[p['name']]
                port_desc["subnet"] = p['fixed_ips'][0]['subnet_id']
                port_desc["ip_address"] = p['fixed_ips'][0]['ip_address']

                # Not all ports are associated with a floating IP, so
                # we need to check first. 
                if port_desc["ip_address"] in ip_map:
                    port_desc["floating_ip"] = ip_map[port_desc["ip_address"]]
        return stack_desc

    def _create_app_stack(self, cluster_uuid, num_instances, security_group_ports, assign_floating_ip, ctype):
        """
        Create an empty application stack. This includes the instances, 
        security groups, and floating IPs. 
        """

        logging.info("creating security group for %s" % cluster_uuid)
        sec_group_plan, sec_group_desc = self._create_security_plan(cluster_uuid = cluster_uuid,
                                                                      ports = security_group_ports)

        logging.info("creating instances for %s" % cluster_uuid)
        stack_plan, stack_desc = self._create_instance_plan(cluster_uuid = cluster_uuid, 
                                                            num_instances = num_instances, 
                                                            image = self.default_image,
                                                            size = self.default_personality, 
                                                            sec_group_name = sec_group_desc.keys()[0], 
                                                            ctype = ctype)

        # See if we need to assign any floating IPs 
        # for this stack. We need the references to the neutron
        # port which is contained in the description. 
        if assign_floating_ip:
            logging.info("creating floating IPs for %s" % cluster_uuid)
            ifaces = []
            for k in stack_desc.keys():
                if stack_desc[k]["type"] == "OS::Neutron::Port" and stack_desc[k]["role"] == "manage":
                    ifaces.append(k)
            ip_plan, ip_desc = self._create_floatingip_plan(cluster_uuid = cluster_uuid,
                                                            ifaces = ifaces)
        else:
            ip_plan = { "Resources" : {}}
            ip_desc = {}

        # Now we need to combine all these plans and
        # launch the cluster. 
        stack_plan["Resources"] = dict(sec_group_plan["Resources"].items() + 
                                       ip_plan["Resources"].items() + 
                                       stack_plan["Resources"].items())
        stack_desc = dict(stack_desc.items() + 
                          sec_group_desc.items() +
                          ip_desc.items())
        stack_desc = self._launch_heat_plan("ferry-app-%s-%s" % (ctype.upper(), cluster_uuid), stack_plan, stack_desc)

        # Now find all the IP addresses of the various
        # machines. 
        if stack_desc:
            return self._collect_network_info(stack_desc)

    def _get_private_ip(self, server, subnet_id, resources):
        """
        Get the IP address associated with the supplied server. 
        """
        for port_name in server["ports"]:
            port_desc = resources[port_name]
            if port_desc["subnet"] == subnet_id:
                return port_desc["ip_address"]

    def _get_public_ip(self, server, resources):
        """
        Get the IP address associated with the supplied server. 
        """
        for port_name in server["ports"]:
            port_desc = resources[port_name]
            if "floating_ip" in port_desc:
                return port_desc["floating_ip"]

    def _get_net_info(self, server_info, subnet, resources):
        """
        Look up the IP address, gateway, and subnet range. 
        """
        cidr = subnet["cidr"].split("/")[1]
        ip = self._get_private_ip(server_info, subnet["id"], resources)

        # We want to use the host NIC, so modify LXC to use phys networking, and
        # then start the docker containers on the server. 
        lxc_opts = ["lxc.network.type = phys",
                    "lxc.network.ipv4 = %s/%s" % (ip, cidr),
                    "lxc.network.ipv4.gateway = %s" % subnet["gateway"],
                    "lxc.network.link = %s" % self.data_network,
                    "lxc.network.name = eth0", 
                    "lxc.network.flags = up"]
        return lxc_opts, ip

    def alloc(self, cluster_uuid, container_info, ctype, proxy):
        """
        Allocate a new cluster. 
        """

        # Now take the cluster and create the security group
        # to expose all the right ports. 
        sec_group_ports = []
        if ctype == "connector": 
            # Since this is a connector, we need to expose
            # the public ports. For now, we ignore the host port. 
            floating_ip = True
            for c in container_info:
                for p in c['ports']:
                    s = str(p).split(":")
                    if len(s) > 1:
                        sec_group_ports.append( (s[1], s[1]) )
                    else:
                        sec_group_ports.append( (s[0], s[0]) )
        else:
            if proxy:
                # If the controller is acting as a proxy, then it has
                # direct access to the VMs, so the backend shouldn't
                # get any floating IPs. 
                floating_ip = False
            else:
                # Otherwise, the backend should also get floating IPs
                # so that the controller can access it. 
                floating_ip = True

            # We need to create a range tuple, so check if 
            # the exposed port is a range.
            for p in container_info[0]['exposed']:
                s = p.split("-")
                if len(s) == 1:
                    sec_group_ports.append( (s[0], s[0]) )
                else:
                    sec_group_ports.append( (s[0], s[1]) )

        # Tell OpenStack to allocate the cluster. 
        resources = self._create_app_stack(cluster_uuid = cluster_uuid, 
                                           num_instances = len(container_info), 
                                           security_group_ports = sec_group_ports,
                                           assign_floating_ip = floating_ip,
                                           ctype = ctype)
        
        # Now we need to ask the cluster to start the 
        # Docker containers.
        containers = []
        mounts = {}

        if resources:
            self.apps[cluster_uuid] = resources
            servers = self._get_servers(resources)
            for i in range(0, len(container_info)):
                # Fetch a server to run the Docker commands. 
                server = servers[i]

                # Get the LXC networking options
                lxc_opts, private_ip = self._get_net_info(server, self.subnet, resources)

                # Now get an addressable IP address. Normally we would use
                # a private IP address since we should be operating in the same VPC.
                public_ip = self._get_public_ip(server, resources)
                self._copy_public_keys(public_ip)
                container, cmounts = self.controller.execute_docker_containers(container_info[i], lxc_opts, private_ip, public_ip)
                
                if container:
                    mounts = dict(mounts.items() + cmounts.items())
                    containers.append(container)

        # # Check if we need to set the file permissions
        # # for the mounted volumes. 
        # for c, i in mounts.items():
        #     for _, v in i['vols']:
        #         self.cmd([c], 'chown -R %s %s' % (i['user'], v))

        return containers
        
    def _delete_stack(self, stack_id):
        # To delete the stack properly, we first need to disassociate
        # the floating IPs. 
        ips = []
        resources = self._collect_resources(stack_id)
        for r in resources:
            if r["resource_type"] == "OS::Neutron::FloatingIP":
                self.neutron.update_floatingip(r["physical_resource_id"], {'floatingip': {'port_id': None}})

        # Now delete the stack. 
        self.heat.stacks.delete(stack_id)