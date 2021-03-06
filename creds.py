#!/usr/bin/env python2

from os import geteuid, devnull
import logging
# shut up scapy
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
from scapy.all import *
conf.verb=0
from sys import exit
import binascii
import struct
import argparse
import signal
import base64
from urllib import unquote
from subprocess import Popen, PIPE
from collections import OrderedDict
from BaseHTTPServer import BaseHTTPRequestHandler
from StringIO import StringIO
from urllib import unquote
import binascii
from time import gmtime, strftime

logging.basicConfig(filename='credentials.txt',level=logging.INFO)
DN = open(devnull, 'w')
pkt_frag_loads = OrderedDict()
challenge_acks = OrderedDict()
mail_auths = OrderedDict()
telnet_stream = OrderedDict()

# Regexs
authenticate_re = '(www-|proxy-)?authenticate'
authorization_re = '(www-|proxy-)?authorization'
ftp_user_re = r'USER (.+)\r\n'
ftp_pw_re = r'PASS (.+)\r\n'
irc_user_re = r'NICK (.+?)((\r)?\n|\s)'
irc_pw_re = r'NS IDENTIFY (.+)'
irc_pw_re2 = 'nickserv :identify (.+)'
mail_auth_re = '(\d+ )?(auth|authenticate) (login|plain)'
mail_auth_re1 =  '(\d+ )?login '
NTLMSSP2_re = 'NTLMSSP\x00\x02\x00\x00\x00.+'
NTLMSSP3_re = 'NTLMSSP\x00\x03\x00\x00\x00.+'
http_search_re = '((search|query|&q|\?q|search\?p|searchterm|keywords|keyword|command|terms|keys|question|kwd|searchPhrase)=([^&][^&]*))'
W = '\033[0m'  # white (normal)
T = '\033[93m'  # tan

def parse_args():

   parser = argparse.ArgumentParser()
   parser.add_argument("-i", "--interface", help="Choose an interface")
   parser.add_argument("-p", "--pcap", help="Parse info from a pcap file; -p <pcapfilename>")
   parser.add_argument("-f", "--filterip", help="Do not sniff packets from this IP address; -f 192.168.0.4")
   parser.add_argument("-v", "--verbose", help="Display entire URLs and POST loads rather than truncating at 100 characters", action="store_true")
   return parser.parse_args()

def iface_finder():
    try:
        ipr = Popen(['/sbin/ip', 'route'], stdout=PIPE, stderr=DN)
        for line in ipr.communicate()[0].splitlines():
            if 'default' in line:
                l = line.split()
                iface = l[4]
                return iface
    except IOError:
        exit('[-] Could not find an internet active interface; please specify one with -i <interface>')

def frag_remover(ack, load):

    global pkt_frag_loads


    while len(pkt_frag_loads) > 50:
        pkt_frag_loads.popitem(last=False)


    copy_pkt_frag_loads = copy.deepcopy(pkt_frag_loads)
    for ip_port in copy_pkt_frag_loads:
        if len(copy_pkt_frag_loads[ip_port]) > 0:

            while len(copy_pkt_frag_loads[ip_port]) > 25:
                pkt_frag_loads[ip_port].popitem(last=False)


    copy_pkt_frag_loads = copy.deepcopy(pkt_frag_loads)
    for ip_port in copy_pkt_frag_loads:

        for ack in copy_pkt_frag_loads[ip_port]:

            if len(copy_pkt_frag_loads[ip_port][ack]) > 5000:
                pkt_frag_loads[ip_port][ack] = pkt_frag_loads[ip_port][ack][-200:]

def frag_joiner(ack, src_ip_port, load):

    for ip_port in pkt_frag_loads:
        if src_ip_port == ip_port:
            if ack in pkt_frag_loads[src_ip_port]:

                old_load = pkt_frag_loads[src_ip_port][ack]
                concat_load = old_load + load
                return OrderedDict([(ack, concat_load)])

    return OrderedDict([(ack, load)])

def pkt_parser(pkt):

    global pkt_frag_loads, mail_auths

    if pkt.haslayer(Raw):
        load = pkt[Raw].load

    if pkt.haslayer(Ether) and pkt.haslayer(Raw) and not pkt.haslayer(IP) and not pkt.haslayer(IPv6):
        return

    if pkt.haslayer(UDP) and pkt.haslayer(IP) and pkt.haslayer(Raw):

        src_ip_port = str(pkt[IP].src) + ':' + str(pkt[UDP].sport)
        dst_ip_port = str(pkt[IP].dst) + ':' + str(pkt[UDP].dport)

        if pkt.haslayer(SNMP):
            parse_snmp(src_ip_port, dst_ip_port, pkt[SNMP])
            return

        decoded = Decode_Ip_Packet(str(pkt)[14:])
        kerb_hash = ParseMSKerbv5UDP(decoded['data'][8:])
        if kerb_hash:
            printer(src_ip_port, dst_ip_port, kerb_hash)

    elif pkt.haslayer(TCP) and pkt.haslayer(Raw) and pkt.haslayer(IP):

        ack = str(pkt[TCP].ack)
        seq = str(pkt[TCP].seq)
        src_ip_port = str(pkt[IP].src) + ':' + str(pkt[TCP].sport)
        dst_ip_port = str(pkt[IP].dst) + ':' + str(pkt[TCP].dport)
        frag_remover(ack, load)
        pkt_frag_loads[src_ip_port] = frag_joiner(ack, src_ip_port, load)
        full_load = pkt_frag_loads[src_ip_port][ack]


        if 0 < len(full_load) < 750:


            ftp_creds = parse_ftp(full_load, dst_ip_port)
            if len(ftp_creds) > 0:
                for msg in ftp_creds:
                    printer(src_ip_port, dst_ip_port, msg)
                return

            mail_creds_found = mail_logins(full_load, src_ip_port, dst_ip_port, ack, seq)


            irc_creds = irc_logins(full_load, pkt)
            if irc_creds != None:
                printer(src_ip_port, dst_ip_port, irc_creds)
                return


            telnet_logins(src_ip_port, dst_ip_port, load, ack, seq)

        other_parser(src_ip_port, dst_ip_port, full_load, ack, seq, pkt, parse_args().verbose)

def telnet_logins(src_ip_port, dst_ip_port, load, ack, seq):

    global telnet_stream

    msg = None

    if src_ip_port in telnet_stream:

        try:
            telnet_stream[src_ip_port] += load.decode('utf8')
        except UnicodeDecodeError:
            pass


        if '\r' in telnet_stream[src_ip_port] or '\n' in telnet_stream[src_ip_port]:
            telnet_split = telnet_stream[src_ip_port].split(' ', 1)
            cred_type = telnet_split[0]
            value = telnet_split[1].replace('\r\n', '').replace('\r', '').replace('\n', '')

            msg = 'Telnet %s: %s' % (cred_type, value)
            printer(src_ip_port, dst_ip_port, msg)
            del telnet_stream[src_ip_port]

    if len(telnet_stream) > 100:
        telnet_stream.popitem(last=False)
    mod_load = load.lower().strip()
    if mod_load.endswith('username:') or mod_load.endswith('login:'):
        telnet_stream[dst_ip_port] = 'username '
    elif mod_load.endswith('password:'):
        telnet_stream[dst_ip_port] = 'password '

def ParseMSKerbv5TCP(Data):

    try:
        MsgType = Data[21:22]
        EncType = Data[43:44]
        MessageType = Data[32:33]
    except IndexError:
        return

    if MsgType == "\x0a" and EncType == "\x17" and MessageType =="\x02":
        if Data[49:53] == "\xa2\x36\x04\x34" or Data[49:53] == "\xa2\x35\x04\x33":
            HashLen = struct.unpack('<b',Data[50:51])[0]
            if HashLen == 54:
                Hash = Data[53:105]
                SwitchHash = Hash[16:]+Hash[0:16]
                NameLen = struct.unpack('<b',Data[153:154])[0]
                Name = Data[154:154+NameLen]
                DomainLen = struct.unpack('<b',Data[154+NameLen+3:154+NameLen+4])[0]
                Domain = Data[154+NameLen+4:154+NameLen+4+DomainLen]
                BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
                return 'MS Kerberos: %s' % BuildHash

        if Data[44:48] == "\xa2\x36\x04\x34" or Data[44:48] == "\xa2\x35\x04\x33":
            HashLen = struct.unpack('<b',Data[47:48])[0]
            Hash = Data[48:48+HashLen]
            SwitchHash = Hash[16:]+Hash[0:16]
            NameLen = struct.unpack('<b',Data[HashLen+96:HashLen+96+1])[0]
            Name = Data[HashLen+97:HashLen+97+NameLen]
            DomainLen = struct.unpack('<b',Data[HashLen+97+NameLen+3:HashLen+97+NameLen+4])[0]
            Domain = Data[HashLen+97+NameLen+4:HashLen+97+NameLen+4+DomainLen]
            BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
            return 'MS Kerberos: %s' % BuildHash

        else:
            Hash = Data[48:100]
            SwitchHash = Hash[16:]+Hash[0:16]
            NameLen = struct.unpack('<b',Data[148:149])[0]
            Name = Data[149:149+NameLen]
            DomainLen = struct.unpack('<b',Data[149+NameLen+3:149+NameLen+4])[0]
            Domain = Data[149+NameLen+4:149+NameLen+4+DomainLen]
            BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
            return 'MS Kerberos: %s' % BuildHash

def ParseMSKerbv5UDP(Data):

    try:
        MsgType = Data[17:18]
        EncType = Data[39:40]
    except IndexError:
        return

    if MsgType == "\x0a" and EncType == "\x17":
        try:
            if Data[40:44] == "\xa2\x36\x04\x34" or Data[40:44] == "\xa2\x35\x04\x33":
                HashLen = struct.unpack('<b',Data[41:42])[0]
                if HashLen == 54:
                    Hash = Data[44:96]
                    SwitchHash = Hash[16:]+Hash[0:16]
                    NameLen = struct.unpack('<b',Data[144:145])[0]
                    Name = Data[145:145+NameLen]
                    DomainLen = struct.unpack('<b',Data[145+NameLen+3:145+NameLen+4])[0]
                    Domain = Data[145+NameLen+4:145+NameLen+4+DomainLen]
                    BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
                    return 'MS Kerberos: %s' % BuildHash

                if HashLen == 53:
                    Hash = Data[44:95]
                    SwitchHash = Hash[16:]+Hash[0:16]
                    NameLen = struct.unpack('<b',Data[143:144])[0]
                    Name = Data[144:144+NameLen]
                    DomainLen = struct.unpack('<b',Data[144+NameLen+3:144+NameLen+4])[0]
                    Domain = Data[144+NameLen+4:144+NameLen+4+DomainLen]
                    BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
                    return 'MS Kerberos: %s' % BuildHash

            else:
                HashLen = struct.unpack('<b',Data[48:49])[0]
                Hash = Data[49:49+HashLen]
                SwitchHash = Hash[16:]+Hash[0:16]
                NameLen = struct.unpack('<b',Data[HashLen+97:HashLen+97+1])[0]
                Name = Data[HashLen+98:HashLen+98+NameLen]
                DomainLen = struct.unpack('<b',Data[HashLen+98+NameLen+3:HashLen+98+NameLen+4])[0]
                Domain = Data[HashLen+98+NameLen+4:HashLen+98+NameLen+4+DomainLen]
                BuildHash = "$krb5pa$23$"+Name+"$"+Domain+"$dummy$"+SwitchHash.encode('hex')
                return 'MS Kerberos: %s' % BuildHash
        except struct.error:
            return

def Decode_Ip_Packet(s):

    d={}
    d['header_len']=ord(s[0]) & 0x0f
    d['data']=s[4*d['header_len']:]
    return d

def double_line_checker(full_load, count_str):

    num = full_load.lower().count(count_str)
    if num > 1:
        lines = full_load.count('\r\n')
        if lines > 1:
            full_load = full_load.split('\r\n')[-2] # -1 is ''
    return full_load

def parse_ftp(full_load, dst_ip_port):

    print_strs = []


    full_load = double_line_checker(full_load, 'USER')


    ftp_user = re.match(ftp_user_re, full_load)
    ftp_pass = re.match(ftp_pw_re, full_load)

    if ftp_user:
        msg1 = 'FTP User: %s' % ftp_user.group(1).strip()
        print_strs.append(msg1)
        if dst_ip_port[-3:] != ':21':
            msg2 = 'Nonstandard FTP port, confirm the service that is running on it'
            print_strs.append(msg2)

    elif ftp_pass:
        msg1 = 'FTP Pass: %s' % ftp_pass.group(1).strip()
        print_strs.append(msg1)
        if dst_ip_port[-3:] != ':21':
            msg2 = 'Nonstandard FTP port, confirm the service that is running on it'
            print_strs.append(msg2)

    return print_strs

def mail_decode(src_ip_port, dst_ip_port, mail_creds):

    try:
        decoded = base64.b64decode(mail_creds).replace('\x00', ' ').decode('utf8')
        decoded = decoded.replace('\x00', ' ')
    except TypeError:
        decoded = None
    except UnicodeDecodeError as e:
        decoded = None

    if decoded != None:
        msg = 'Decoded: %s' % decoded
        printer(src_ip_port, dst_ip_port, msg)

def mail_logins(full_load, src_ip_port, dst_ip_port, ack, seq):

    global mail_auths
    found = False

    full_load = double_line_checker(full_load, 'auth')


    if src_ip_port in mail_auths:
        if seq in mail_auths[src_ip_port][-1]:
            stripped = full_load.strip('\r\n')
            try:
                decoded = base64.b64decode(stripped)
                msg = 'Mail authentication: %s' % decoded
                printer(src_ip_port, dst_ip_port, msg)
            except TypeError:
                pass
            mail_auths[src_ip_port].append(ack)

    elif dst_ip_port in mail_auths:
        if seq in mail_auths[dst_ip_port][-1]:

            a_s = 'Authentication successful'
            a_f = 'Authentication failed'

            if full_load.startswith('235') and 'auth' in full_load.lower():

                printer(dst_ip_port, src_ip_port, a_s)
                found = True
                try:
                    del mail_auths[dst_ip_port]
                except KeyError:
                    pass

            elif full_load.startswith('535 '):

                printer(dst_ip_port, src_ip_port, a_f)
                found = True
                try:
                    del mail_auths[dst_ip_port]
                except KeyError:
                    pass

            elif ' fail' in full_load.lower():

                printer(dst_ip_port, src_ip_port, a_f)
                found = True
                try:
                    del mail_auths[dst_ip_port]
                except KeyError:
                    pass

            elif ' OK [' in full_load:

                printer(dst_ip_port, src_ip_port, a_s)
                found = True
                try:
                    del mail_auths[dst_ip_port]
                except KeyError:
                    pass


            else:
                if len(mail_auths) > 100:
                    mail_auths.popitem(last=False)
                mail_auths[dst_ip_port].append(ack)


    else:
        mail_auth_search = re.match(mail_auth_re, full_load, re.IGNORECASE)
        if mail_auth_search != None:
            auth_msg = full_load

            if mail_auth_search.group(1) != None:
                auth_msg = auth_msg.split()[1:]
            else:
                auth_msg = auth_msg.split()

            if len(auth_msg) > 2:
                mail_creds = ' '.join(auth_msg[2:])
                msg = 'Mail authentication: %s' % mail_creds
                printer(src_ip_port, dst_ip_port, msg)

                mail_decode(src_ip_port, dst_ip_port, mail_creds)
                try:
                    del mail_auths[src_ip_port]
                except KeyError:
                    pass
                found = True


            if len(mail_auths) > 100:
                mail_auths.popitem(last=False)
            mail_auths[src_ip_port] = [ack]

        elif re.match(mail_auth_re1, full_load, re.IGNORECASE) != None:


            auth_msg = full_load
            auth_msg = auth_msg.split()
            if 2 < len(auth_msg) < 5:
                mail_creds = ' '.join(auth_msg[2:])
                msg = 'Authentication: %s' % mail_creds
                printer(src_ip_port, dst_ip_port, msg)
                mail_decode(src_ip_port, dst_ip_port, mail_creds)
                found = True

    if found == True:
        return True

def irc_logins(full_load, pkt):

    user_search = re.match(irc_user_re, full_load)
    pass_search = re.match(irc_pw_re, full_load)
    pass_search2 = re.search(irc_pw_re2, full_load.lower())
    if user_search:
        msg = 'IRC nick: %s' % user_search.group(1)
        return msg
    if pass_search:
        msg = 'IRC pass: %s' % pass_search.group(1)
        return msg
    if pass_search2:
        msg = 'IRC pass: %s' % pass_search2.group(1)
        return msg

def other_parser(src_ip_port, dst_ip_port, full_load, ack, seq, pkt, verbose):

    user_passwd = None
    http_url_req = None
    method = None
    http_methods = ['GET ', 'POST ', 'CONNECT ', 'TRACE ', 'TRACK ', 'PUT ', 'DELETE ', 'HEAD ']
    http_line, header_lines, body = parse_http_load(full_load, http_methods)
    headers = headers_to_dict(header_lines)
    if 'host' in headers:
        host = headers['host']
    else:
        host = ''

    if http_line != None:
        method, path = parse_http_line(http_line, http_methods)
        http_url_req = get_http_url(method, host, path, headers)
        if http_url_req != None:
            if verbose == False:
                if len(http_url_req) > 98:
                    http_url_req = http_url_req[:99] + '...'
            printer(src_ip_port, None, http_url_req)


    searched = get_http_searches(http_url_req, body, host)
    if searched:
        printer(src_ip_port, dst_ip_port, searched)


    if body != '':
        user_passwd = get_login_pass(body)
        if user_passwd != None:
            try:
                http_user = user_passwd[0].decode('utf8')
                http_pass = user_passwd[1].decode('utf8')

                if len(http_user) > 75 or len(http_pass) > 75:
                    return
                user_msg = 'HTTP username: %s' % http_user
                printer(src_ip_port, dst_ip_port, user_msg)
                pass_msg = 'HTTP password: %s' % http_pass
                printer(src_ip_port, dst_ip_port, pass_msg)
            except UnicodeDecodeError:
                pass


    if method == 'POST' and 'ocsp.' not in host:
        try:
            if verbose == False and len(body) > 99:

                msg = 'POST load: %s...' % body[:99].encode('utf8')
            else:
                msg = 'POST load: %s' % body.encode('utf8')
            printer(src_ip_port, None, msg)
        except UnicodeDecodeError:
            pass


    decoded = Decode_Ip_Packet(str(pkt)[14:])
    kerb_hash = ParseMSKerbv5TCP(decoded['data'][20:])
    if kerb_hash:
        printer(src_ip_port, dst_ip_port, kerb_hash)


    NTLMSSP2 = re.search(NTLMSSP2_re, full_load, re.DOTALL)
    NTLMSSP3 = re.search(NTLMSSP3_re, full_load, re.DOTALL)
    if NTLMSSP2:
        parse_ntlm_chal(NTLMSSP2.group(), ack)
    if NTLMSSP3:
        ntlm_resp_found = parse_ntlm_resp(NTLMSSP3.group(), seq)
        if ntlm_resp_found != None:
            printer(src_ip_port, dst_ip_port, ntlm_resp_found)


    if len(headers) == 0:
        authenticate_header = None
        authorization_header = None
    for header in headers:
        authenticate_header = re.match(authenticate_re, header)
        authorization_header = re.match(authorization_re, header)
        if authenticate_header or authorization_header:
            break

    if authorization_header or authenticate_header:

        netntlm_found = parse_netntlm(authenticate_header, authorization_header, headers, ack, seq)
        if netntlm_found != None:
            printer(src_ip_port, dst_ip_port, netntlm_found)


        parse_basic_auth(src_ip_port, dst_ip_port, headers, authorization_header)

def get_http_searches(http_url_req, body, host):

    false_pos = ['i.stack.imgur.com']

    searched = None
    if http_url_req != None:
        searched = re.search(http_search_re, http_url_req, re.IGNORECASE)
        if searched == None:
            searched = re.search(http_search_re, body, re.IGNORECASE)

    if searched != None and host not in false_pos:
        searched = searched.group(3)

        try:

            searched = searched.decode('utf8')
        except UnicodeDecodeError:
            return

        if searched in [str(num) for num in range(0,10)]:
            return

        if len(searched) > 100:
            return
        msg = 'Searched %s: %s' % (host, unquote(searched.encode('utf8')).replace('+', ' '))
        return msg

def parse_basic_auth(src_ip_port, dst_ip_port, headers, authorization_header):

    if authorization_header:

        try:
            header_val = headers[authorization_header.group()]
        except KeyError:
            return
        b64_auth_re = re.match('basic (.+)', header_val, re.IGNORECASE)
        if b64_auth_re != None:
            basic_auth_b64 = b64_auth_re.group(1)
            basic_auth_creds = base64.decodestring(basic_auth_b64)
            msg = 'Basic Authentication: %s' % basic_auth_creds
            printer(src_ip_port, dst_ip_port, msg)

def parse_netntlm(authenticate_header, authorization_header, headers, ack, seq):
    '''
    Parse NTLM hashes out
    '''

    if authenticate_header != None:
        chal_header = authenticate_header.group()
        parse_netntlm_chal(headers, chal_header, ack)


    elif authorization_header != None:
        resp_header = authorization_header.group()
        msg = parse_netntlm_resp_msg(headers, resp_header, seq)
        if msg != None:
            return msg

def parse_snmp(src_ip_port, dst_ip_port, snmp_layer):

    if type(snmp_layer.community.val) == str:
        ver = snmp_layer.version.val
        msg = 'SNMPv%d community string: %s' % (ver, snmp_layer.community.val)
        printer(src_ip_port, dst_ip_port, msg)
    return True

def get_http_url(method, host, path, headers):

    if method != None and path != None:


        if host != '' and not re.match('(http(s)?://)?'+host, path):
            http_url_req = method + ' ' + host + path
        else:
            http_url_req = method + ' ' + path

        http_url_req = url_filter(http_url_req)

        return http_url_req

def headers_to_dict(header_lines):

    headers = {}

    headers_list = [x for line in header_lines for x in line.split(': ', 1)]
    headers_dict = dict(zip(headers_list[0::2], headers_list[1::2]))

    for header in headers_dict:
        headers[header.lower()] = headers_dict[header]

    return headers

def parse_http_line(http_line, http_methods):

    http_line_split = http_line.split()
    method = ''
    path = ''


    if len(http_line_split) > 1:
        method = http_line_split[0]
        path = http_line_split[1]


    if method+' ' not in http_methods:
        method = None
        path = None

    return method, path

def parse_http_load(full_load, http_methods):

    try:
        headers, body = full_load.split("\r\n\r\n", 1)
    except ValueError:
        headers = full_load
        body = ''
    header_lines = headers.split("\r\n")


    http_line = get_http_line(header_lines, http_methods)
    if not http_line:
        headers = ''
        body = full_load

    header_lines = [line for line in header_lines if line != http_line]

    return http_line, header_lines, body

def get_http_line(header_lines, http_methods):

    for header in header_lines:
        for method in http_methods:

            if header.startswith(method):
                http_line = header
                return http_line

def parse_netntlm_chal(headers, chal_header, ack):

    try:
        header_val2 = headers[chal_header]
    except KeyError:
        return
    header_val2 = header_val2.split(' ', 1)

    if header_val2[0] == 'NTLM' or header_val2[0] == 'Negotiate':
        msg2 = header_val2[1]
        msg2 = base64.decodestring(msg2)
        parse_ntlm_chal(ack, msg2)

def parse_ntlm_chal(msg2, ack):

    global challenge_acks

    Signature = msg2[0:8]
    try:
        msg_type = struct.unpack("<I",msg2[8:12])[0]
    except Exception:
        return
    assert(msg_type==2)
    ServerChallenge = msg2[24:32].encode('hex')


    if len(challenge_acks) > 50:
        challenge_acks.popitem(last=False)
    challenge_acks[ack] = ServerChallenge

def parse_netntlm_resp_msg(headers, resp_header, seq):

    try:
        header_val3 = headers[resp_header]
    except KeyError:
        return
    header_val3 = header_val3.split(' ', 1)


    if header_val3[0] == 'NTLM' or header_val3[0] == 'Negotiate':
        try:
            msg3 = base64.decodestring(header_val3[1])
        except binascii.Error:
            return
        return parse_ntlm_resp(msg3, seq)

def parse_ntlm_resp(msg3, seq):


    if seq in challenge_acks:
        challenge = challenge_acks[seq]
    else:
        challenge = 'CHALLENGE NOT FOUND'

    if len(msg3) > 43:

        lmlen, lmmax, lmoff, ntlen, ntmax, ntoff, domlen, dommax, domoff, userlen, usermax, useroff = struct.unpack("12xhhihhihhihhi", msg3[:44])
        lmhash = binascii.b2a_hex(msg3[lmoff:lmoff+lmlen])
        nthash = binascii.b2a_hex(msg3[ntoff:ntoff+ntlen])
        domain = msg3[domoff:domoff+domlen].replace("\0", "")
        user = msg3[useroff:useroff+userlen].replace("\0", "")

        if ntlen == 24: #NTLMv1
            msg = '%s %s' % ('NETNTLMv1:', user+"::"+domain+":"+lmhash+":"+nthash+":"+challenge)
            return msg
        elif ntlen > 60: #NTLMv2
            msg = '%s %s' % ('NETNTLMv2:', user+"::"+domain+":"+challenge+":"+nthash[:32]+":"+nthash[32:])
            return msg

def url_filter(http_url_req):

    if http_url_req:
        d = ['.jpg', '.jpeg', '.gif', '.png', '.css', '.ico', '.js', '.svg', '.woff']
        if any(http_url_req.endswith(i) for i in d):
            return

    return http_url_req

def get_login_pass(body):

    user = None
    passwd = None


    userfields = ['log','login', 'wpname', 'ahd_username', 'unickname', 'nickname', 'user', 'user_name',
                  'alias', 'pseudo', 'email', 'username', '_username', 'userid', 'form_loginname', 'loginname',
                  'login_id', 'loginid', 'session_key', 'sessionkey', 'pop_login', 'uid', 'id', 'user_id', 'screename',
                  'uname', 'ulogin', 'acctname', 'account', 'member', 'mailaddress', 'membername', 'login_username',
                  'login_email', 'loginusername', 'loginemail', 'uin', 'sign-in']
    passfields = ['ahd_password', 'pass', 'password', '_password', 'passwd', 'session_password', 'sessionpassword', 
                  'login_password', 'loginpassword', 'form_pw', 'pw', 'userpassword', 'pwd', 'upassword', 'login_password'
                  'passwort', 'passwrd', 'wppassword', 'upasswd']

    for login in userfields:
        login_re = re.search('(%s=[^&]+)' % login, body, re.IGNORECASE)
        if login_re:
            user = login_re.group()
    for passfield in passfields:
        pass_re = re.search('(%s=[^&]+)' % passfield, body, re.IGNORECASE)
        if pass_re:
            passwd = pass_re.group()

    if user and passwd:
        return (user, passwd)

def printer(src_ip_port, dst_ip_port, msg):
    if dst_ip_port != None:
        print_str = '[%s%s : %s%s] -> %s %s %s' % ('\033[93m', src_ip_port, dst_ip_port, '\033[0m', T, msg, W)


        skip = ['Searched ', 'POST load:']
        for s in skip:
            if s not in msg:
                if os.path.isfile('credentials.txt'):
                    with open('credentials.txt', 'r') as log:
                        contents = log.read()
                        if msg in contents:
                            return

        print print_str


        ansi_escape = re.compile(r'\x1b[^m]*m')
        print_str = ansi_escape.sub('', print_str)


        logging.info(print_str)
    else:
	    	sltime = strftime("%H:%M:%S", gmtime())
        	print_str = '[%s] %s:%s' % (src_ip_port.split(':')[0], sltime, msg)
        	print print_str

def main(args):

    if args.pcap:
        try:
            for pkt in PcapReader(args.pcap):
                pkt_parser(pkt)
        except IOError:
            exit('[-] Could not open %s' % args.pcap)

    else:

        if geteuid():
            exit('[-] Please run as root')


        if args.interface:
            conf.iface = args.interface
        else:
            conf.iface = iface_finder()
        print '[*] Using interface:', conf.iface

        if args.filterip:
            sniff(iface=conf.iface, prn=pkt_parser, filter="not host %s" % args.filterip, store=0)
        else:
            sniff(iface=conf.iface, prn=pkt_parser, store=0)


if __name__ == "__main__":
   main(parse_args())
