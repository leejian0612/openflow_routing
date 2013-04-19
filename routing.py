import gevent
import time


from ryu.base import app_manager
from ryu.controller.handler import set_ev_handler, set_ev_cls
from ryu.controller.handler import (HANDSHAKE_DISPATCHER, MAIN_DISPATCHER,
                                    CONFIG_DISPATCHER, DEAD_DISPATCHER)
from ryu.controller import ofp_event
from ryu import topology
from ryu.ofproto import ofproto_v1_0, nx_match
from ryu.ofproto import ether, inet
from ryu.lib.packet import (packet, ethernet, arp, icmp, icmpv6, ipv4, ipv6,
                            tcp, udp)
from ryu.lib import mac

from switch import Port, Switch
from util import read_cfg
import algorithm
import convert

class Routing(app_manager.RyuApp):
    ARP_TIMEOUT = 600    # in seconds

    FLOW_IDLE_TIMEOUT = 60
    FLOW_HARD_TIMEOUT = 600

    def __init__(self, *args, **kwargs):
        super(Routing, self).__init__(*args, **kwargs)
        
        self.dpid_to_switch = {}    # dpid_to_switch[dpid] = Switch
                                    # maintains all the switches
        self.ip_to_mac = {}     # ip_to_mac[ip_addr] = (mac_addr, 
                                #                       time_stamp)
                                # maintains ARP table
        self.routing_algo = algorithm.Dijkstra(self.dpid_to_switch)

        self.filepath = 'config.xml'

        try:
            self.switch_cfg = read_cfg(self.filepath)
            #print self.switch_cfg
        except:
            print "File %s Parse Error" % self.filepath

            #gevent.spawn(self._test)

    def _test(self):
        while True:
            self.__test()
            gevent.sleep(3)

    def __test(self):
        print '-------------------'
        for k, switch in self.dpid_to_switch.iteritems():
            print switch, switch.name
            for k, port in switch.ports.iteritems():
                print port

        print '-------------------'


    def _pre_install_flow_entry(self, switch):
        # 'switch' is a Switch object
        pass

    @set_ev_handler(topology.event.EventSwitchEnter)
    def switch_enter_handler(self, event):
        # very strangely, EventSwitchEnter happens after 
        # EventOFPSwitchFeatures sometimes
        dpid = event.switch.dp.id
        try:
            s = self.dpid_to_switch[dpid]
        except KeyError:
            s = Switch(event.switch.dp)
            self.dpid_to_switch[dpid] = s

        self._pre_install_flow_entry(s)

    @set_ev_handler(topology.event.EventSwitchLeave)
    def switch_leave_handler(self, event):
        try:
            del self.dpid_to_switch[event.switch.dp.id]
        except KeyError:
            pass


    def _update_port_link(self, dpid, port):
        switch = self.dpid_to_switch[dpid]
        p = switch.ports.get(port.port_no, None)
        if p:
            p.peer_switch_dpid = port.peer_switch_dpid
            p.peer_port_no = port.peer_port_no
        else:
            switch.ports[port.port_no] = port

        peer_switch = self.dpid_to_switch[port.peer_switch_dpid]
        switch.peer_to_local_port[peer_switch] = port.peer_port_no


    @set_ev_handler(topology.event.EventLinkAdd)
    def link_add_handler(self, event):
        src_port = Port(port = event.link.src, peer = event.link.dst)
        dst_port = Port(port = event.link.dst, peer = event.link.src)
        self._update_port_link(src_port.dpid, src_port)
        self._update_port_link(dst_port.dpid, dst_port)

    def _delete_link(self, port):
        try:
            switch = self.dpid_to_switch[port.dpid]
            p = switch.ports[port.port_no]
        except KeyError:
            return

        p.peer_switch_dpid = None
        p.peer_port_no = None

    @set_ev_handler(topology.event.EventLinkDelete)
    def link_delete_handler(self, event):
        try:
            switch_1 = self.dpid_to_switch[event.link.src.dpid]
            switch_2 = self.dpid_to_switch[event.link.dst.dpid]
            del switch_1.peer_to_local_port[switch_2]
            del switch_2.peer_to_local_port[switch_1]
        except KeyError:
            return

        self._delete_link(event.link.src)
        self._delete_link(event.link.dst)


    @set_ev_handler(topology.event.EventPortAdd)
    def port_add_handler(self, event):
        port = Port(event.port)
        switch = self.dpid_to_switch[port.dpid]
        switch.ports[port.port_no] = port

    @set_ev_handler(topology.event.EventPortDelete)
    def port_delete_handler(self, event):
        port = Port(event.port)
        try:
            switch = self.dpid_to_switch[port.dpid]
            del switch.ports[port.port_no]
        except KeyError:
            pass


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, [MAIN_DISPATCHER, 
                                                CONFIG_DISPATCHER])
    # we must handle this event because ryu's topology discovery
    # only shows ports between switches
    def switch_feature_handler(self, event):
        dpid = event.msg.datapath_id
        try:
            switch = self.dpid_to_switch[dpid]
        except KeyError:
            self.dpid_to_switch[dpid] = Switch(event.msg.datapath)

        switch = self.dpid_to_switch[dpid]
        for port_no, port in event.msg.ports.iteritems():
            if port_no not in switch.ports:
                p = Port(port = port, dp = event.msg.datapath)
                switch.ports[p.port_no] = p

            if port_no == ofproto_v1_0.OFPP_LOCAL:
                switch.name = port.name.rstrip('\x00')
            else:
                # port.curr is a number of 32 bits, only used 12 bits in ovs
                # represents current features of the port.
                # LOCAL port doesn't have
                curr = port.curr & 0x7f	# get last 7 bits
                p.cost = 64/curr
                print p.cost

        switch.update_from_config(self.switch_cfg)

    def find_packet(self, pkt, target):
        for packet in pkt.protocols:
            if packet.protocol_name == target:
                return packet
        print "can't find target_packet!"
        return None


    def _handle_arp(self, msg, pkt, arp_pkt):
        '''
            handles ARP request from host, about their gateways;
            no need to handle other types of ARP packets, only request;
            only works in IPv4 since IPv6 uses NDP(ICMPv6);
            e.g. when a host need to send a packet to the gateway, it will
                firstly send an ARP to get the MAC address of the gateway
        '''
        # XXX
        # should handle ARP reply about hosts,
        # i.e. reply to _send_arp_request()
        print 'arp', arp_pkt

        if arp_pkt.opcode != arp.ARP_REQUEST:
            return

        switch = self.dpid_to_switch[msg.datapath.id]
        in_port_no = msg.in_port
        req_dst_ip = arp_pkt.dst_ip
        req_src_ip = arp_pkt.src_ip
        
        port = switch.ports[in_port_no]
        if port.gateway and req_dst_ip != port.gateway.gw_ip:
            return

        datapath = msg.datapath
        reply_src_mac = port.hw_addr
        ether_layer = self.find_packet(pkt, 'ethernet')

        e = ethernet.ethernet(dst = ether_layer.src, src = reply_src_mac,
                                ethertype = ether.ETH_TYPE_ARP)
        a = arp.arp(hwtype = arp.ARP_HW_TYPE_ETHERNET, 
                    proto = ether.ETH_TYPE_IP,
                    hlen = 6, plen = 4, opcode = arp.ARP_REPLY, 
                    src_mac = reply_src_mac, src_ip = req_dst_ip,
                    dst_mac = arp_pkt.src_mac, dst_ip = req_src_ip)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a) 
        p.serialize()             
                        
        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
                actions = [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                data = p.data)

        print "arp request packet's dst_mac is ", reply_src_mac


    def _handle_icmp(self, msg, pkt, icmp_pkt):
        '''
            reply to ICMP_ECHO_REQUEST(i.e. ping);
            may handle other types of ICMP msg in the future;
            return True if send a responce
        '''
        print 'icmp', icmp_pkt
        if icmp_pkt.type != icmp.ICMP_ECHO_REQUEST:
            return False

        in_port_no = msg.in_port
        switch = self.dpid_to_switch[msg.datapath.id]
        ipv4_layer = self.find_packet(pkt, 'ipv4')
        ip_src = ipv4_layer.src
        ip_dst = ipv4_layer.dst
        
        need_reply = False
        for _k, p in switch.ports.iteritems():
            if p.gateway and p.gateway.gw_ip == ip_dst:
                need_reply = True
                break
        if not need_reply:
            return False
        
        echo_id = icmp_pkt.data.id
        echo_seq = icmp_pkt.data.seq
        echo_data = bytearray(icmp_pkt.data.data)

        icmp_data = icmp.echo(id_=echo_id,seq=echo_seq,data=echo_data)
        
        #send a echo reply packet
        ether_layer = self.find_packet(pkt, 'ethernet') 
        ether_dst = ether_layer.src
        ether_src = switch.ports[in_port_no].hw_addr
        e = ethernet.ethernet(ether_dst,ether_src,ether.ETH_TYPE_IP)
        #csum calculation should be paied attention
        i = ipv4.ipv4(version=4,header_length=5,tos=0,total_length=0,
            identification=0,flags=0x000,offset=0,ttl=64,proto=1,csum=0,
            src=ip_dst,dst=ip_src,option=None)
        ic = icmp.icmp(type_= 0,code=0,csum=0,data=icmp_data)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(i)
        p.add_protocol(ic)
        p.serialize()
        datapath = msg.datapath
        datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                actions=[datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                data=p.data)
        print 'send a ping replay'
        return True

    def _handle_icmpv6(self, msg, pkt, icmpv6_pkt):
        print 'icmpv6', icmpv6_pkt
        #XXX
        # should handle icmpv6.ND_NEIGHBOR_ADVERT,
        # i.e. replay of _send_icmp_NS()

        switch = self.dpid_to_switch[msg.datapath.id]
        in_port_no = msg.in_port

        if icmpv6_pkt.type_ == icmpv6.ND_NEIGHBOR_SOLICIT:
            port = switch.ports[in_port_no]
            if port.gateway and icmpv6_pkt.data.dst != port.gateway.gw_ipv6:
                return False
            #send a ND_NEIGHBOR_REPLY packet
            ether_layer = self.find_packet(pkt, 'ethernet')
            ether_dst = ether_layer.src
            ether_src = port.hw_addr
            e = ethernet.ethernet(ether_dst,ether_src,ether.ETH_TYPE_IPV6)
            ic6_data_data = icmpv6.nd_option_la(hw_src=ether_src, data=None)
            #res = 3 or 7
            ic6_data = icmpv6.nd_neighbor(res=3,dst=icmpv6_pkt.data.dst,
                    type_=icmpv6.nd_neighbor.ND_OPTION_TLA,length=1,
                    data=ic6_data_data)
            ic6 = icmpv6.icmpv6(type_=icmpv6.ND_NEIGHBOR_ADVERT,code=0,
                    csum=0,data=ic6_data)  
            #payload_length
            ipv6_pkt = self.find_packet(pkt, 'ipv6')
            i6 = ipv6.ipv6(version= 6,traffic_class=0,flow_label=0,
                    payload_length=32,nxt=58,hop_limit=255,
                    src=icmpv6_pkt.data.dst,dst=ipv6_pkt.src)
            p = packet.Packet()
            p.add_protocol(e)
            p.add_protocol(i6)
            p.add_protocol(ic6)
            p.serialize()
            datapath = msg.datapath
            datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                    actions=
                        [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                    data=p.data)
            print 'send a NA packet'
        elif icmpv6_pkt.type_ == icmpv6.ICMPV6_ECHO_REQUEST:
            ipv6_pkt = self.find_packet(pkt, 'ipv6')
            
            need_reply = False
            for _k, p in switch.ports.iteritems():
                if p.gateway and p.gateway.gw_ipv6 == ipv6_pkt.dst:
                    need_reply = True
                    break
            if not need_reply:
                return
            
            ether_layer = self.find_packet(pkt, 'ethernet')
            ether_dst = ether_layer.src
            ether_src = switch.ports[in_port_no].hw_addr
            e = ethernet.ethernet(ether_dst,ether_src,ether.ETH_TYPE_IPV6)
            ic6_data = icmpv6_pkt.data
            ic6 = icmpv6.icmpv6(type_=icmpv6.ICMPV6_ECHO_REPLY,code=0,
                                csum=0,data=ic6_data)
            i6 = ipv6.ipv6(version= 6,traffic_class=0,flow_label=0,
                            payload_length=64,nxt=58,hop_limit=64,
                            src=ipv6_pkt.dst,dst=ipv6_pkt.src)
            p = packet.Packet()
            p.add_protocol(e)
            p.add_protocol(i6)
            p.add_protocol(ic6)
            p.serialize()
            datapath = msg.datapath
            datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                    actions=
                        [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                    data=p.data)
            print 'send a ping6 reply packet'

        return True

    def _remember_mac_addr(self, packet, _4or6):
        '''
            get ip <-> mac relationship from packets and 
            store them in dict ip_to_mac
        '''
        time_now = time.time()
        ether_layer = self.find_packet(packet, 'ethernet')
        if _4or6 == 4:
            ip_layer = self.find_packet(packet, 'ipv4')
        else:
            ip_layer = self.find_packet(packet, 'ipv6')
        self.ip_to_mac[ip_layer.src] = (ether_layer.src, time_now)
        

    def deploy_flow_entry(self, msg, switch_list, _4or6):
        '''
            deploy flow entry into switch
        '''
        # TODO
        # this method and last_switch_out should be restructured
        length = len(switch_list)
        for i in xrange(length - 1):
            this_switch = switch_list[i]
            next_switch = switch_list[i + 1]
            outport_no = this_switch[next_switch]
            if _4or6 == 4:
                ip_layer = self.find_packet('ipv4')
            else:
                ip_layer = self.find_packet('ipv6')

            ip_dst = ip_layer.dst
            outport = this_switch.ports[outport_no]
            mac_src = outport.hw_addr
            mac_dst = next_switch.ports[outport.peer_port_no].hw_addr
            if _4or6 == 4:
                # ip src exact match
                wildcards = ofproto_v1_0.OFPFW_ALL
                wildcards &= ~ofproto_v1_0.OFPFW_DL_TYPE
                wildcards &= ~(0x3f << ofproto_v1_0.OFPFW_NW_DST_SHIFT)

                match = dp.ofproto_parser.OFPMatch(
                        # because of wildcards, parameters other than dl_type
                        # and nw_dst could be any value
                        wildcards = wildcards, in_port = 0, 
                        dl_src = 0, dl_dst = 0, dl_vlan = 0, dl_vlan_pcp = 0,
                        dl_type = ether.ETH_TYPE_IP, nw_tos = 0, nw_proto = 0,
                        nw_src = 0, nw_dst = ip_dst, tp_src = 0,
                        tp_dst = 0)
            else:
                rule = nx_match.ClsRule()
                rule.set_dl_type(ether.ETH_TYPE_IPV6)
                rule.set_ipv6_dst(ip_dst)

            actions = []
            actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                            mac_src))
            actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                            mac_dst))
            actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))

            if _4or6 == 4:
                mod = dp.ofproto_parser.OFPFlowMod(
                    datapath = this_switch.datapath, match = match,
                    cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, actions = actions)
            else:
                mod = dp.ofproto_parser.NXTFlowMod(
                        datapath = this_switch.datapath, cookie = 0, 
                        command = dp.ofproto.OFPFC_MODIFY,
                        idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                        hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                        out_port = outport_no, rule = rule,
                        actions = actions)

            this_switch.datapath.send_msg(mod)

        # send packet out from the first switch
        switch = switch_list[0]
        next_switch = switch_list[1]
        outport_no = this_switch[next_switch]
        if _4or6 == 4:
            ip_layer = self.find_packet('ipv4')
        else:
            ip_layer = self.find_packet('ipv6')

        ip_dst = ip_layer.dst
        outport = this_switch.ports[outport_no]
        mac_src = outport.hw_addr
        mac_dst = next_switch.ports[outport.peer_port_no].hw_addr
        actions = []
        actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                        mac_src))
        actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                        mac_dst))
        actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))
                
        out = dp.ofproto_parser.OFPPacketOut(
            datapath = dp, buffer_id = msg.buffer_id,
            in_port = msg.in_port, actions = actions)
        
        switch.datapath.send_msg(out)


    def _send_arp_request(self, datapath, outport_no, dst_ip):
        src_mac_addr = \
            self.dpid_to_switch[datapath.id].ports[outport_no].hw_addr
        src_ip = \
            self.dpid_to_switch[datapath.id].ports[outport_no].gateway.gw_ip
        p = packet.Packet()
        e = ethernet.ethernet(dst = mac.haddr_to_bin(mac.BROADCAST),
            src = src_mac_addr, ethertype = ether.ETH_TYPE_ARP)
        p.add_protocol(e)
        a = arp.arp_ip(opcode = arp.ARP_REQUEST, src_mac = src_mac_addr,
                src_ip = src_ip, dst_mac = mac.haddr_to_bin(mac.DONTCARE),
                dst_ip = dst_ip)
        p.add_protocol(a)
        p.serialize()

        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
            actions = [datapath.ofproto_parser.OFPActionOutput(outport_no)],
            data = p.data)

    def _generate_dst_for_NS(self, ipv6_addr):
        '''
            ICMPv6 neighbor solicitation destination addresses in ethernet
            and IP layer are multicast addresses, and could be generated as:
            
            IPv6:
                ff02::1:ffXX:XXXX
            where XX is the last 24 bits of the target IPv6 address
            
            ethernet:
                33:33:XX:XX:XX:XX
            where XX is the last 32 bits of the IPv6 multicast address,
            i.e. the address generated above, so the effective ethernet
            multicast address in this scenario is:
                33:33:ff:XX:XX:XX

            Ref: RFC 2464, RFC 2373
        '''
        args = convert.ipv6_to_arg_list(ipv6_addr)

        arg_6 = args[6] & 0x00ff
        arg_7head = ('%04x' % args[7])[0:2]
        arg_7tail = ('%04x' % args[7])[2:]
        ethernet_str = '33:33:ff:' + str(arg_6) + ':' + arg_7head + ':' + \
                       arg_7tail
        ethernet_addr = convert.haddr_to_bin(ethernet_str)

        args[6] = args[6] | 0xff00
        args[0:6] = [0xff02,0,0,0,0,1]
        ip_addr = convert.arg_list_to_ipv6_bin(args)
        return ethernet_addr, ip_addr

    def _send_icmp_NS(self, datapath, outport_no, dst_ip):
        src_mac_addr = \
            self.dpid_to_switch[datapath.id].ports[outport_no].hw_addr
        src_ip = \
            self.dpid_to_switch[datapath.id].ports[outport_no].gateway.gw_ipv6
        p = packet.Packet()
        dst_mac, dst_ip_multicast = self._generate_dst_for_NS(dst_ip)
        e = ethernet.ethernet(dst = dst_mac, src = src_mac_addr,
                ethertype = ether.ETH_TYPE_IPV6)
        ip6 = ipv6.ipv6(version = 6, traffic_class = 0, flow_label = 0,
                # 4byte ICMP header, 4byte reserved, 16byte target address,
                # 8byte "source link-layer address" option
                # next header value for ICMPv6 is 58
                payload_length = 32, nxt = 58, hop_limit = 255,
                src = src_ip, dst = dst_ip_multicast)
        # source link-layer address
        sla_addr = icmpv6.nd_option_la(hw_addr = src_mac_addr)
        # ns for neighbor solicit, res for reserved
        ns = icmpv6.nd_neighbor(res = 0, dst = dst_ip,
                    type_ = icmpv6.nd_neighbor.ND_OPTION_SLA,
                    length = 1, data = sla_addr)
        ic6 = icmpv6.icmpv6(type_ = icmpv6.ND_NEIGHBOR_SOLICIT, code = 0,
                # checksum = 0 then ryu calculate for you
                csum = 0, data = ns)
        p.add_protocol(e)
        p.add_protocol(ip6)
        p.add_protocol(ic6)
        p.serialize()
        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
            actions = [datapath.ofproto_parser.OFPActionOutput(outport_no)],
            data = p.data)


    def last_switch_out(self, msg, outport_no, _4or6):
        if _4or6 == 4:
            ip_layer = self.find_packet('ipv4')
        else:
            ip_layer = self.find_packet('ipv6')

        try:
            mac_addr = self.ip_to_mac[ip_layer.dst]
        except KeyError:
            if _4or6 == 4:
                self._send_arp_request(msg.datapath, outport_no, 
                                        ip_layer.dst)
            else:
                self._send_icmp_NS(msg.datapath, outport_no,
                                    ip_layer.dst)
            return

        dp = msg.datapath
        switch = self.dpid_to_switch[dp.id]

        if _4or6 == 4:
            # ip src exact match
            wildcards = ofproto_v1_0.OFPFW_ALL
            wildcards &= ~ofproto_v1_0.OFPFW_DL_TYPE
            wildcards &= ~(0x3f << ofproto_v1_0.OFPFW_NW_DST_SHIFT)

            match = dp.ofproto_parser.OFPMatch(
                    # because of wildcards, parameters other than dl_type
                    # and nw_dst could be any value
                    wildcards = wildcards, in_port = 0, 
                    dl_src = 0, dl_dst = 0, dl_vlan = 0, dl_vlan_pcp = 0,
                    dl_type = ether.ETH_TYPE_IP, nw_tos = 0, nw_proto = 0,
                    nw_src = 0, nw_dst = ip_layer.dst, tp_src = 0,
                    tp_dst = 0)
        else:
            rule = nx_match.ClsRule()
            rule.set_dl_type(ether.ETH_TYPE_IPV6)
            rule.set_ipv6_dst(ip_layer.dst)

        actions = []
        actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                        switch.ports[outport_no].hw_addr))
        actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                        mac_addr))
        actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))
        
        if _4or6 == 4:
            mod = dp.ofproto_parser.OFPFlowMod(
                    datapath = dp, match = match, cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, actions = actions)
        else:
            mod = dp.ofproto_parser.NXTFlowMod(
                    datapath = dp, cookie = 0, 
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, rule = rule,
                    actions = actions)
 
        out = dp.ofproto_parser.OFPPacketOut(
            datapath = dp, buffer_id = msg.buffer_id,
            in_port = msg.in_port, actions = actions)

        dp.send_msg(mod)
        dp.send_msg(out)
            

    def find_switch_of_network(self, dst_addr, _4or6):
        for dpid, switch in self.dpid_to_switch.iteritems():
            for port_no, port in switch.ports.iteritems():
                if _4or6 == 4:
                    if port.gateway and convert.ipv4_in_network(dst_addr,
                                    port.gateway.gw_ip, 
                                    port.gateway.prefixlen):
                        return self.dpid_to_switch[dpid], port_no
                else:
                    if port.gateway and convert.ipv6_in_network(dst_addr,
                                    port.gateway.gw_ipv6,
                                    port.gateway.ipv6prefixlen):
                        return self.dpid_to_switch[dpid], port_no
        return None, None

    def _handle_ip(self, msg, pkt, protocol_pkt):
        print 'ip', protocol_pkt

        if isinstance(protocol_pkt, ipv4.ipv4):
            _4or6 = 4
        else:
            _4or6 = 6

        self._remember_mac_addr(pkt, _4or6)

        if _4or6 == 4:
            try:
                icmp_layer = self.find_packet(pkt, 'icmp')
                if self._handle_icmp(msg, pkt, icmp_layer):
                    # if icmp method handles this packet successfully,
                    # further processing is not needed
                    return
            except:
                pass
        else: # _4or6 == 6
            try:
                icmpv6_layer = self.find_packet(pkt, 'icmpv6')
                if self._handle_icmpv6(msg, pkt, icmpv6_layer):
                    return
            except:
                pass
        
        src_switch = self.dpid_to_switch[msg.datapath.id]
        dst_switch, dst_port_no = self.find_switch_of_network(
                                        protocol_pkt.dst, 4)
        if src_switch == dst_switch:
            self.last_switch_out(msg, dst_port_no, _4or6)
            return
        elif dst_switch == None:
            # can't find destination in this domain
            # raise an event to `moudle B`
            # XXX
            pass

        result = self.routing_algo.find_route(src_switch, dst_switch)
        if result:
            self.deploy_flow_entry(msg, result, _4or6)
        else:
            self.drop_pkt(msg)

    def drop_pkt(self, msg):
        # Note that this drop_pkt method only drops the packet,
        # does not install any flow entries
        dp = msg.datapath
        out = dp.ofproto_parser.OFPPacketOut(datapath = dp,
                buffer_id = msg.buffer_id, in_port = msg.in_port,
                actions = [])
        dp.send_msg(out)
        

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, event):
        data = event.msg.data
        pkt = packet.Packet(data)
        for p in pkt.protocols:
            if isinstance(p, arp.arp):
                self._handle_arp(event.msg, pkt, p)
            # ipv4 and ipv6 also handle their corresponding icmp packets
            elif isinstance(p, ipv4.ipv4):
                self._handle_ip(event.msg, pkt, p)
            elif isinstance(p, ipv6.ipv6):
                self._handle_ip(event.msg, pkt, p)
            else:
                # might be more classifications here, BGP/OSPF etc.
                pass
