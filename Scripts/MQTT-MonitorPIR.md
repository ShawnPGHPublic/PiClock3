# Wire a PIR to Raspberry PI

Wiring (read the labels on the PCB)Pin order is often VCC – OUT – GND, but some boards reverse VCC/GND. Trust the silkscreen, not photos.
WWZMDiB pin		Raspberry Pi
VCC			5V (physical pin 2 or 4)
GND			GND (physical pin 6)
OUT			GPIO17 / BCM 17 (physical pin 11)

Do not feed 5V into a GPIO. OUT on these modules is normally ~3.3 V and is safe on a Pi input.On the module:Jumper H — OUT stays high while motion continues (better for “occupied”)
Jumper L — one pulse per detection
Left pot — sensitivity  
Right pot — ON time (a few seconds to minutes)

Allow 30–60 seconds after power-up; these boards dump false triggers while they settle.


Use BCM numbers in the script, not physical pin numbers.MQTTTopic
Payload
When
home/pc/motion
ON
motion starts
home/pc/motion
OFF
PIR drops (no motion)

Retained, so Home Assistant can treat it as a binary sensor.bash

mosquitto_sub -h localhost -t home/pc/motion -v

If gpiozero is missing:bash

sudo apt install python3-gpiozero


