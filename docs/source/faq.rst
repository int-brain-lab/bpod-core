FAQ
===

I can't connect to a Bpod on Linux
----------------------------------
By default, Linux restricts access to USB serial devices to root and members of the ``dialout`` group for security reasons.
Additionally, system services like ``ModemManager`` automatically probe serial devices to detect modems, which can interfere with normal communication.
Bpod devices use Teensy microcontrollers, which appear as USB serial devices (``/dev/ttyACM*``) that are subject to these restrictions.

Installing the `Teensy UDEV Rules <https://www.pjrc.com/teensy/00-teensy.rules>`__ should solve these issues by:

- Granting all users read/write access to Teensy devices without requiring ``dialout`` group membership
- Preventing ModemManager from probing and interfering with the connection
- Configuring serial ports with appropriate low-level settings (raw mode, no echo)

Without these rules, you may experience:

- "Permission denied" errors when trying to open the serial port
- Slow or unreliable connections due to ModemManager interference
- Missing data or timeout errors during communication
