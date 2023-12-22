# Audio/Video Setup

Regardless of what operating system or device you're using there will likely be some gremlins in the audio setup.  This page exists to help you troubleshoot those items.

NOTICE: The information on this page is primarily focused at folks using __PulseAudio__ as their sound engine.

## Listing Audio Devices by Device ID
Inevitably you are going to need to know your device IDs for your input and output devices.  The good news is that it is relatively simple _once Pulse Audio is installed_.  Here are the commands.
```
## List Microphones/Inputs
pacmd list-sources | grep -e "index:" -e device.string -e "name:" 

## List Outputs
pacmd list-sinks | grep -e "index:" -e device.string -e "name:"
```
Once you have what you think to be the correct device IDs you can test it out with one or more of the following commands.
```
## Testing audio output
paplay -d 1 /usr/share/sounds/alsa/Front_Center.wav

## Testing audio input/recording
parecord -d 1 test.wav
```

## Picking a voice for Text-to-Speech (Festival)
Next we need to configure output to Festival.  Festival will reference a file in the user's home directory just like the .bashrc above.  You can also set this in /etc, but we'll keep it simple with the following:
```
echo "(Parameter.set 'Audio_Required_Format 'aiff)" > ~/.festivalrc
echo "(Parameter.set 'Audio_Command \"paplay $FILE --client-name=Festival --stream-name=Speech -d 1\")" >> ~/.festivalrc
echo "(Parameter.set 'Audio_Method 'Audio_Command)" >> ~/.festivalrc
echo "(set! voice_default 'voice_cmu_us_slt_arctic_hts)" >> ~/.festivalrc
```
BE ADVISED that you must know the device index to use the above.  Change the "-d 1" to the index number of the device you're using for output from pulse audio.  You can omit this setting if you set the default devices inside the control panels, but if you are using multiple devices that can be a little cumbersome so I've opted to use the device IDs to be sure I'm pointing to the desired output/input device.  

You can convert any text you want into voice using festival's --tts switch.  It works as follows:
```
echo "Testing speech" | festival --tts
```

## EXTRA: Install/Compile a Console-Based Visualizer
During research on how to provide a visual interface for Kenzy we ran across a cool console-based audio visualizer that's easy to set up and use and does a fanstastic job of making the whole thing come to life.  This isn't part of Kenzy specifically.  It's just cool and we wanted to share.

The project is hosted on Github as [dpayne/cli-visualizer](https://github.com/dpayne/cli-visualizer)

### Required Libraries
```
sudo apt-get install libfftw3-dev libncursesw5-dev cmake libpulse-dev xterm
```

### Compile/Install the Visualizer
```
cd /tmp
wget https://github.com/dpayne/cli-visualizer/archive/master.zip
unzip master.zip
cd cli-visualizer-master

export ENABLE_PULSE=1
./install.sh
```

You may need to set it to the specific audio device.  You can add the device's ID in the "~/.config/vis/config" file.  (Make sure to use the correct ID.  Here I'm referencing ID by the index 1.)
```
echo "audio.pulse.source=1" >> ~/.config/vis/config
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)