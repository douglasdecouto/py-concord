
import sys
import threading
import time

from concord import AlarmPanelInterface




class FakeSerial(object):
    def __init__(self, msg_list_):
        self.msg_list = msg_list_
        self.curr_msg_idx = 0
        self.curr_char_idx = 0

    def ck_msg_avail(self):
        if self.curr_msg_idx >= len(self.msg_list):
            raise StopIteration("No more fake messages to send")

    def write(self, c):
        print "WROTE: %r" % c

    # Dummy for testing, will make sure panel driver code always tries
    # to read to end of available fake messages.
    def inWaiting(self):
        return 1

    def read1(self):
        self.ck_msg_avail()
        curr_msg = self.msg_list[self.curr_msg_idx]
        while len(curr_msg) == 0:
            self.curr_msg_idx += 1
            self.curr_char_idx = 0
            self.ck_msg_avail()
            curr_msg = self.msg_list[self.curr_msg_idx]
        b = curr_msg[self.curr_char_idx]
        self.curr_char_idx += 1
        if self.curr_char_idx >= len(curr_msg):
            self.curr_char_idx = 0
            self.curr_msg_idx += 1
        # print "XXXX %r, 0x%02x" % (b, ord(b))
        return b

    def read(self, size=1):
        b = '';
        for i in range(size):
            b += self.read1()
        return b

    def close(self):
        pass

class FakeLog(object):
    def __init__(self, f):
        self.f = f
    def log(self, s):
        self.f.write(s + "\n")
    def debug_msg(self, s):
        self.log(s)
    def error(self, s):
        self.log(s)

def old_main():
    messages = [
        '\n020204',
        '\n037a9b18', # not a real command, but checksum example from docs
        ]

    # These messages have blank checksums that need to be updated (00
    # at end) plus linefeeds need to be prepended.
    messages2 = [
        '082201040000500300',  # Arming level
        '0721050000a71900', # Zone status
        '0d22020600020102030409050600', # Alarm/trouble
        '090304001100ff020400', # zone data, no zone text
        '0c0304001100ff02046e574600', # zone data, with zone text
        '0b0114030202040000000700',
        ]

    for m in messages2:
        bin_msg = decode_message_from_ascii(m)
        update_message_checksum(bin_msg)
        ascii_msg = encode_message_to_ascii(bin_msg)
        messages.append('\n' + ascii_msg)

    if len(sys.argv) == 1:
        # fake test mode
        panel = AlarmPanelInterface("fake", 0.010, FakeLog(sys.stdout))
        panel.serial_interface.serdev = FakeSerial(messages)
        try:
            panel.message_loop()
        except StopIteration:
            pass
        print "No more fake messages"

    else:
        ser_dev_name = sys.argv[1]
        panel = AlarmPanelInterface(ser_dev_name, 0.1, FakeLog(sys.stdout))
        if len(sys.argv) > 2:
            # transmit mode -- act as dummy panel for testing
            # purposes.
            if False:
                the_thread = threading.Thread(target=panel.message_loop)
                the_thread.start()
                for m in messages:
                    print m
                    bin_msg = decode_message_from_ascii(m[1:])
                    panel.enqueue_msg_for_tx(bin_msg[:-1]) # panel method will work out checksum
                    time.sleep(0.25)
                    continue
            else:
                for m in messages:
                    time.sleep(1)
                    panel.serial_interface.serdev.write(m)
                    print m
                    c = ''
                    while len(c) < 1:
                        time.sleep(0.25)
                        c = panel.serial_interface.serdev.read(1)
                    if c == ACK:
                        print "OK"
                    else:
                        print "Error, got 0x%02x" % ord(c)


        else:
            # listen mode
            def zone_handler(msg):
                print "ZONE_HANDLER: %r - %r" % (msg['zone_number'], msg['zone_state'])
            def zone_data_handler(msg):
                print "ZONE DATA: %r / %s / %r" % (msg['zone_number'], msg['zone_text'], msg['zone_state'])

            panel.register_message_handler('ZONE_STATUS', zone_handler)
            panel.register_message_handler('ZONE_DATA', zone_data_handler)
            panel.message_loop()



def main():
    assert len(sys.argv) >= 2
    dev_name = sys.argv[1]

    panel = AlarmPanelInterface(dev_name, 0.1, FakeLog(sys.stdout))
    
    t = threading.Thread(target=panel.message_loop)
    t.start()
    
    try:
        while True:
            l = sys.stdin.readline()
            l = l.strip()
            if len(l) < 1:
                continue
            cmd = l[0]
            x = 1
            if len(l) > 1:
                try:
                    x = int(l[1:])
                except ValueError:
                    print "Bad extra param"
            if cmd == '*':
                print "SEND * part=%d" % x
                # Can't actually send the '*' key to any partitions...
                panel.send_keypress([0x0a], partition=x)
            elif cmd == 'c':
                print "SEND CHIME part=%d" % x
                # Partitions > 1 work for this
                panel.send_keypress([7, 1], partition=x)
            elif cmd == 'r':
                print "SEND REFRESH"
                panel.request_dynamic_data_refresh()
            elif cmd == 'l':
                print "SEND REQUEST ALL EQUIPMENT LIST"
                panel.request_all_equipment()
            elif cmd == 'u':
                print "SEND REQUEST USERS"
                panel.request_zones()
            elif cmd == 'q':
                break # Quit!
            else:
                print "???? %r" % cmd
    except KeyboardInterrupt:
        print "CAUGHT ^C, exiting"
    
    panel.stop_loop()
    t.join()


if __name__ == '__main__':
    main()
