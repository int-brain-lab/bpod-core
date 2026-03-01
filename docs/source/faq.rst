Frequently Asked Questions
==========================

I can't connect to a Bpod on Linux
----------------------------------
If you're experiencing "Permission denied" errors, slow or unreliable connections,
missing data, or timeout errors when trying to connect to your Bpod on Linux, you likely
need to install additional udev rules.

By default, Linux restricts access to USB serial devices to root and members of the
``dialout`` group for security reasons. Additionally, system services like
`ModemManager <https://modemmanager.org/>`__  automatically probe serial devices to
detect modems, which can interfere with normal communication. Bpod devices use
`Teensy microcontrollers <https://www.pjrc.com/teensy/>`__, which appear as USB serial
devices that are subject to these restrictions.

Installing the `Teensy udev rules <https://www.pjrc.com/teensy/00-teensy.rules>`__
should solve these issues by:

- Granting all users read/write access to Teensy devices without requiring ``dialout``
  group membership
- Preventing `ModemManager <https://modemmanager.org/>`__  from probing and interfering
  with the connection
- Configuring serial ports with appropriate low-level settings (raw mode, no echo)
