# Copyright 2014: Rackspace UK
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json

from rally.benchmark.context import keypair
from rally.benchmark.scenarios import base
from rally.benchmark.scenarios.cinder import utils as cinder_utils
from rally.benchmark.scenarios.nova import utils as nova_utils
from rally.benchmark.scenarios.vm import utils as vm_utils
from rally.benchmark import types as types
from rally.benchmark import validation
from rally.benchmark.wrappers import network as network_wrapper
from rally import consts
from rally import exceptions
import threading
import time
import re


class VMTasks(nova_utils.NovaScenario, vm_utils.VMScenario,
              cinder_utils.CinderScenario):
    """Benchmark scenarios that are to be run inside VM instances."""

    def __init__(self, *args, **kwargs):
        super(VMTasks, self).__init__(*args, **kwargs)

    @types.set(image=types.ImageResourceType,
               flavor=types.FlavorResourceType)
    @validation.image_valid_on_flavor("flavor", "image")
    @validation.file_exists("script")
    @validation.number("port", minval=1, maxval=65535, nullable=True,
                       integer_only=True)
    @validation.external_network_exists("floating_network")
    @validation.required_services(consts.Service.NOVA, consts.Service.CINDER)
    @validation.required_openstack(users=True)
    @base.scenario(context={"cleanup": ["nova", "cinder"],
                            "keypair": {}, "allow_ssh": {}})
    def boot_runcommand_delete(self, image, flavor,
                               script, interpreter, username,
                               password=None,
                               volume_args=None,
                               floating_network=None,
                               port=22,
                               force_delete=False,
                               **kwargs):
        """Boot a server, run a script that outputs JSON, delete the server.

        Example Script in samples/tasks/support/instance_dd_test.sh

        :param image: glance image name to use for the vm
        :param flavor: VM flavor name
        :param script: script to run on server, must output JSON mapping
                       metric names to values (see the sample script below)
        :param interpreter: server's interpreter to run the script
        :param username: ssh username on server, str
        :param password: Password on SSH authentication
        :param volume_args: volume args for booting server from volume
        :param floating_network: external network name, for floating ip
        :param port: ssh port for SSH connection
        :param force_delete: whether to use force_delete for servers
        :param **kwargs: extra arguments for booting the server
        :returns: dictionary with keys `data' and `errors':
                  data: dict, JSON output from the script
                  errors: str, raw data from the script's stderr stream
        """

        if volume_args:
            volume = self._create_volume(volume_args["size"], imageRef=None)
            kwargs["block_device_mapping"] = {"vdrally": "%s:::1" % volume.id}

        fip = server = None
        net_wrap = network_wrapper.wrap(self.clients)
        kwargs.update({"auto_assign_nic": True,
                       "key_name": keypair.Keypair.KEYPAIR_NAME})
        server = self._boot_server(
            self._generate_random_name("rally_novaserver_"),
            image, flavor, **kwargs)

        if not server.networks:
            raise RuntimeError(
                "Server `%(server)s' is not connected to any network. "
                "Use network context for auto-assigning networks "
                "or provide `nics' argument with specific net-id." % {
                    "server": server.name})

        internal_network = server.networks.keys()[0]
        fixed_ip = server.addresses[internal_network][0]["addr"]
        try:
            fip = net_wrap.create_floating_ip(ext_network=floating_network,
                                              int_network=internal_network,
                                              tenant_id=server.tenant_id,
                                              fixed_ip=fixed_ip)

            self._associate_floating_ip(server, fip["ip"],
                                        fixed_address=fixed_ip)

            code, out, err = self.run_command(fip["ip"], port, username,
                                              password, interpreter, script)
            if code:
                raise exceptions.ScriptError(
                    "Error running script %(script)s."
                    "Error %(code)s: %(error)s" % {
                        "script": script, "code": code, "error": err})
            try:
                data = json.loads(out)
            except ValueError as e:
                raise exceptions.ScriptError(
                    "Script %(script)s has not output valid JSON: "
                    "%(error)s" % {"script": script, "error": str(e)})

            return {"data": data, "errors": err}

        finally:
            if server:
                if fip:
                    if self.check_ip_address(fip["ip"])(server):
                        self._dissociate_floating_ip(server, fip["ip"])
                    net_wrap.delete_floating_ip(fip["id"], wait=True)
                self._delete_server(server, force=force_delete)

    @types.set(image=types.ImageResourceType,
               flavor=types.FlavorResourceType)
    @validation.image_valid_on_flavor("flavor", "image")
    @validation.file_exists("clientscript")
    @validation.file_exists("serverscript")
    @validation.number("port", minval=1, maxval=65535, nullable=True,
                       integer_only=True)
    @validation.external_network_exists("floating_network")
    @validation.required_services(consts.Service.NOVA, consts.Service.CINDER)
    @validation.required_openstack(users=True)
    @base.scenario(context={"cleanup": ["nova", "cinder"],
                            "keypair": {}, "allow_ssh": {}})
    def boot_runcommand_on_two_servers_delete(self, image, flavor,
                               clientscript, serverscript, interpreter,
                               username,
                               password=None,
                               volume_args=None,
                               floating_network=None,
                               port=22,
                               force_delete=False,
                               **kwargs):
        """Boot 2 servers, run a script that outputs JSON, delete the server.

        Example Script in doc/samples/tasks/support/instance_dd_test.sh

        :param image: glance image name to use for the vm
        :param flavor: VM flavor name
        :param clientscript: script to run on iperf client VM, must output JSON mapping
                       metric names to values (see the sample script below)
        :param serverscript: script to run on iperf server VM, must output JSON mapping
               metric names to values
        :param interpreter: server's interpreter to run the script
        :param username: ssh username on server, str
        :param password: Password on SSH authentication
        :param volume_args: volume args for booting server from volume
        :param floating_network: external network name, for floating ip
        :param port: ssh port for SSH connection
        :param force_delete: whether to use force_delete for servers
        :param **kwargs: extra arguments for booting the server
        :returns: dictionary with keys `data' and `errors':
                  data: dict, JSON output from the script
                  errors: str, raw data from the script's stderr stream
        """

        if volume_args:
            volume = self._create_volume(volume_args["size"], imageRef=None)
            kwargs["block_device_mapping"] = {"vda": "%s:::1" % volume.id}
            print "volume.id: ", volume.id


        net_wrap = network_wrapper.wrap(self.clients)
        kwargs.update({"auto_assign_nic": True,
                       "key_name": keypair.Keypair.KEYPAIR_NAME})

        iperf_client = self._boot_server(
            self._generate_random_name("rally_iperf_client_"),
            image, flavor, **kwargs)

        iperf_server = self._boot_server(
            self._generate_random_name("rally_iperf_server_"),
            image, flavor, **kwargs)


        if not iperf_client.networks:
            raise RuntimeError(
                "Server `%(server)s' is not connected to any network. "
                "Use network context for auto-assigning networks "
                "or provide `nics' argument with specific net-id." % {
                    "server": iperf_client.name})
        if not iperf_server.networks:
            raise RuntimeError(
                "Server `%(server)s' is not connected to any network. "
                "Use network context for auto-assigning networks "
                "or provide `nics' argument with specific net-id." % {
                    "server": iperf_server.name})

        client_internal_network = iperf_client.networks.keys()[0]
        server_internal_network = iperf_server.networks.keys()[0]

        client_fixed_ip = iperf_client.addresses[client_internal_network][0]["addr"]
        server_fixed_ip = iperf_server.addresses[server_internal_network][0]["addr"]

        # ysboychakov: update client script file to allow client get the server's ip
        with open(clientscript, 'r+') as file:
            content = file.read()
            ip_pattern = "iperf -c (\d{1,3}[.]){3}\d{1,3}"
            ip_value = "iperf -c " + server_fixed_ip
            new_content = re.sub(ip_pattern, ip_value, content)
            file.seek(0)
            file.truncate()
            file.write(new_content)

        try:
            client_floating_ip = net_wrap.create_floating_ip(ext_network=floating_network,
                                              int_network=client_internal_network,
                                              tenant_id=iperf_client.tenant_id,
                                              fixed_ip=client_fixed_ip)
            server_floating_ip = net_wrap.create_floating_ip(ext_network=floating_network,
                                              int_network=server_internal_network,
                                              tenant_id=iperf_server.tenant_id,
                                              fixed_ip=server_fixed_ip)

            self._associate_floating_ip(iperf_client, client_floating_ip["ip"],
                                        fixed_address=client_fixed_ip)
            self._associate_floating_ip(iperf_server, server_floating_ip["ip"],
                                        fixed_address=server_fixed_ip)

            # ysboychakov: Queue instance to get results out of server thread
            res_q = Queue.Queue()

            def runclient():
                time.sleep(20)
                code, out, err = self.run_command(client_floating_ip["ip"], port, username,
                                              password, interpreter, clientscript)

            def runserver():
                print " runserver() called. Time: ", time.strftime("%H:%M:%S")
                code, out, err = self.run_command(server_floating_ip["ip"], port, username,
                                              password, interpreter, serverscript)
                res_q.put(out)
                res_q.put(code)
                res_q.put(err)


            th1 = threading.Thread(None, runserver, None)
            th2 = threading.Thread(None, runclient, None)


            #ysboychakov: run server first
            th1.start()
            th2.start()

            th1.join()
            th2.join()

            #ysboychakov: ensure no ScriptError raised
            code = 0;

            server_response = res_q.get()
            code = res_q.get()
            err = res_q.get()

            r_pattern = 'sec .* GBytes (.*) [G,M]bits/sec'
            iperf_result = ""
            match = re.search(r_pattern, server_response)
            if match:
                iperf_result = match.group(1)

            out = '{"network performance" : ' + iperf_result + ' }'

            if code:
                raise exceptions.ScriptError(
                    "Error running script %(script)s."
                    "Error %(code)s: %(error)s" % {
                        "script": clientscript, "code": code, "error": err})
            try:
                data = json.loads(out)
            except ValueError as e:
                raise exceptions.ScriptError(
                    "Script %(script)s has not output valid JSON: "
                    "%(error)s" % {"script": clientscript, "error": str(e)})



            return {"data": data, "errors": err}

        finally:

            if iperf_client:
                if client_floating_ip:
                    if self.check_ip_address(client_floating_ip["ip"])(iperf_client):
                        self._dissociate_floating_ip(iperf_client, client_floating_ip["ip"])
                    net_wrap.delete_floating_ip(client_floating_ip["id"], wait=True)
                self._delete_server(iperf_client, force=force_delete)

            if iperf_server:
                if server_floating_ip:
                    if self.check_ip_address(server_floating_ip["ip"])(iperf_client):
                        self._dissociate_floating_ip(iperf_server, client_floating_ip["ip"])
                    net_wrap.delete_floating_ip(server_floating_ip["id"], wait=True)
                self._delete_server(iperf_server, force=force_delete)