
20 October 2013
Douglas S. J. De Couto
decouto@alum.mit.edu

This code is for talking to / hearing from a GE/InterLogix Concord
alarm panel using the Superbus Automoation Module RS-232 interface.

I don't actually have this interface so the code is based solely on
reading the protocol documentation, and may be completely wrong.  Your
fedback appreciated.

Usage:

 # Just do a dry run to exercise some code
 python concord.py

 # Listen to a Concord panel connect to /dev/cu.usbserial
 python concord.py /dev/cu.usbserial

 # Send some fake messages as if we were a Concord panel, using /dev/cu.usbserial
 # Useful for testing the receiving code over a null-model cable
 python concord.py /dev/cu.usbserial tx


RELEASES

July 2014 - v0.3.3
     - Options for less noisy logging of touchpad messages


LICENSE and COPYRIGHT:

This code is Copyright 2013, 2014 by Douglas S. J. De Couto

It is made availble to you under a BSD license; see the file LICENSE
for more details.