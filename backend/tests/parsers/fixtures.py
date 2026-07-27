"""Raw command output captured from the PNETLab Cisco IOSv / vIOS-L2 images."""

IP_INTERFACE_BRIEF = """
Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         10.255.0.2      YES NVRAM  up                    up
GigabitEthernet0/1         10.10.10.11     YES NVRAM  up                    up
GigabitEthernet0/2         unassigned      YES NVRAM  administratively down down
GigabitEthernet0/3         unassigned      YES unset  down                  down
Loopback0                  1.1.1.1         YES NVRAM  up                    up
Vlan1                      unassigned      YES NVRAM  administratively down down
"""

IP_INTERFACE_BRIEF_WIDE_SPACING = """
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0       10.255.0.2        YES    NVRAM     up             up
"""

VLAN_BRIEF = """
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3, Gi1/0, Gi1/1, Gi1/2
                                                Gi1/3, Gi2/0, Gi2/1
10   SALES                            active    Gi0/1, Gi0/2
20   ENGINEERING                      active    Gi0/3
25   MARKETING                        active
99   MANAGEMENT                       active    Gi1/0
1002 fddi-default                     act/unsup
1003 token-ring-default               act/unsup
"""

IP_ROUTE = """
Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2

Gateway of last resort is 10.255.0.1 to network 0.0.0.0

S*    0.0.0.0/0 [1/0] via 10.255.0.1
      1.0.0.0/32 is subnetted, 1 subnets
C        1.1.1.1 is directly connected, Loopback0
      10.0.0.0/8 is variably subnetted, 6 subnets, 3 masks
C        10.10.10.0/24 is directly connected, GigabitEthernet0/1
L        10.10.10.11/32 is directly connected, GigabitEthernet0/1
O        10.10.20.0/24 [110/2] via 10.255.0.6, 00:14:32, GigabitEthernet0/2
O IA     10.10.30.0/24 [110/3] via 10.255.0.6, 00:14:32, GigabitEthernet0/2
O E2     172.16.0.0/16 [110/20] via 10.255.0.10, 00:09:11, GigabitEthernet0/3
"""

OSPF_NEIGHBOR = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.255.0.6      GigabitEthernet0/2
3.3.3.3           1   FULL/BDR        00:00:36    10.255.0.10     GigabitEthernet0/3
4.4.4.4           0   FULL/  -        00:00:31    10.255.0.14     GigabitEthernet0/1
5.5.5.5           1   2WAY/DROTHER    00:00:38    10.255.0.18     GigabitEthernet1/0
"""

OSPF_NEIGHBOR_EMPTY = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
"""

GARBAGE = "% Invalid input detected at '^' marker.\n"
