# KENZY.Ai &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy)](https://github.com/lnxusr1/kenzy/blob/master/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/yt2mp3.svg) ![Read the Docs](https://img.shields.io/readthedocs/kenzy) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy)

This project is dedicated to building a "Synthetic Human" which is called Kenzy for which we have assigned the female gender pronoun of "she". She has intent determination ([padatious](https://github.com/MycroftAI/padatious)) visual face recognition ([opencv/opencv](https://github.com/opencv/opencv)), speech transcription ([whisper](https://openai.com/research/whisper)), and speech synthesis ([speecht5](https://github.com/microsoft/SpeechT5)/[festival](http://www.cstr.ed.ac.uk/projects/festival/).

Visit our main site: [https://kenzy.ai/](https://kenzy.ai/)

## Kenzy's Architecture

Kenzy's architecture is divided into compartments.  These compartments come with two main components:  Servers and Devices.  The servers focus on communication between other compartments and devices are designed to control input and output operations.  Devices are always run within a server and a server can execute only one device.  Servers talk to other servers using HTTP/HTTPS like standard web requests making customizing the communication fairly straightforward.  The most important device is the ```kenzy.skillmanager``` which is a special type of device that collects data and provides the skill engine for reacting to inputs.

All options, configurations, and startup parameters are driven configuration files.  There are a few examples available in the repository under the examples folder.

__Python Module Overview__

| Class/Object         | Description                                                           |
| :------------------- | :-------------------------------------------------------------------- |
| kenzy.core           | Core logic with inheritable objects for each device.                  |
| kenzy.extras         | Extra functions for UPNP/SSDP and other features.                     |
| kenzy.skillmanager   | Core skill manager (a.k.a. "The Brain")                               |
| kenzy.image          | Object/Face detection processing video capture (previously "Watcher") |
| kenzy.tts            | Text-to-speech models processing audio-output (previously "Speaker")  |
| kenzy.stt            | Speech-to-text models processing audio-input (previously "Listener")  |

## Installation

The quickest and easiest way to install Kenzy is to use our installation script:

```
wget -q -O install.sh https://kenzy.ai/installer && sh install.sh
```

Running the script exactly as shown above will install Kenzy and all components.  If you want to be more selective you can add options as follows:

* ```-b``` = Install skill manager dependencies (formerly the "Brain")
* ```-l``` = Install stt dependencies (formerly the "Listener")
* ```-s``` = Install tts dependencies (formerly the "Speaker")
* ```-w``` = Install image dependencies (formerly the "Watcher")
* ```-v [PATH]``` = Python virtual environment path (will create new if does not already exist)

Installer script has been tested on Ubuntu 22.04+ and Debian Buster.

Kenzy is available through pip, but to use the built-in devices there are a few extra libraries you may require.  Please visit the [Basic Install](https://docs.kenzy.ai/en/latest/installation.basic/) page for more details.  

__&raquo; [HOWTO: Install](https://docs.kenzy.ai/en/latest/installation.basic/)__

## Web Control Panel

If everything is working properly you should be able to point your device to the web control panel running on the __skillmanager__ device to test it out.  The default URL is:

__&raquo; [http://localhost:9700/](http://localhost:9700/)__

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)
