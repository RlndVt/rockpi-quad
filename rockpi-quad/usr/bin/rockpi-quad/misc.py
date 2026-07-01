#!/usr/bin/env python3
import re
import os
import time
import subprocess
import multiprocessing as mp
import traceback

import gpiod
from configparser import ConfigParser
from collections import defaultdict, OrderedDict

cmds = {
    'blk': "lsblk | awk '{print $1}'",
    'up': "echo Uptime: `uptime | sed 's/.*up \\([^,]*\\), .*/\\1/'`",
    'temp': "cat /sys/class/thermal/thermal_zone0/temp",
    'ip': "hostname -I | awk '{printf \"IP %s\", $1}'",
    'cpu': "uptime | awk '{printf \"CPU Load: %.2f\", $(NF-2)}'",
    'men': "free -m | awk 'NR==2{printf \"Mem: %s/%sMB\", $3,$2}'",
    'disk': "df -h | awk '$NF==\"/\"{printf \"Disk: %d/%dGB %s\", $3,$2,$5}'"
}

lv2dc = OrderedDict({'lv3': 0, 'lv2': 0.25, 'lv1': 0.5, 'lv0': 0.75})
sata_lines = []
GPIOD_V2 = hasattr(gpiod, 'LineSettings')


def gpiochip_path(chip):
    chip = str(chip)
    if chip.startswith('/dev/'):
        return chip
    if chip.startswith('gpiochip'):
        return f'/dev/{chip}'
    return f'/dev/gpiochip{chip}'


def request_input_line(chip_name, line_number, consumer, pull_up=False):
    line_number = int(line_number)
    if GPIOD_V2:
        settings = {
            'direction': gpiod.line.Direction.INPUT,
        }
        if pull_up:
            settings['bias'] = gpiod.line.Bias.PULL_UP
        chip = gpiod.Chip(gpiochip_path(chip_name))
        request = chip.request_lines(
            config={line_number: gpiod.LineSettings(**settings)},
            consumer=consumer,
        )
        return chip, request

    chip = gpiod.Chip(str(chip_name))
    line = chip.get_line(line_number)
    request = {
        'consumer': consumer,
        'type': gpiod.LINE_REQ_DIR_IN,
    }
    if pull_up and hasattr(gpiod, 'LINE_REQ_FLAG_BIAS_PULL_UP'):
        request['flags'] = gpiod.LINE_REQ_FLAG_BIAS_PULL_UP
    line.request(**request)
    return chip, line


def read_line(handle, line_number):
    line_number = int(line_number)
    if GPIOD_V2:
        return int(handle.get_value(line_number) == gpiod.line.Value.ACTIVE)
    return handle.get_value()


def release_line(chip, handle):
    try:
        handle.release()
    finally:
        if hasattr(chip, 'close'):
            chip.close()


def check_output(cmd):
    return subprocess.check_output(cmd, shell=True).decode().strip()


def check_call(cmd):
    return subprocess.check_call(cmd, shell=True)


def get_blk():
    conf['disk'] = [x for x in check_output(cmds['blk']).strip().split('\n') if x.startswith('sd')]


def get_info(s):
    return check_output(cmds[s])


def get_cpu_temp():
    t = float(get_info('temp')) / 1000
    if conf['oled']['f-temp']:
        temp = "CPU Temp: {:.0f}°F".format(t * 1.8 + 32)
    else:
        temp = "CPU Temp: {:.1f}°C".format(t)
    return temp


def read_conf():
    conf = defaultdict(dict)

    try:
        cfg = ConfigParser()
        cfg.read('/etc/rockpi-quad.conf')
        # fan
        conf['fan']['lv0'] = cfg.getfloat('fan', 'lv0')
        conf['fan']['lv1'] = cfg.getfloat('fan', 'lv1')
        conf['fan']['lv2'] = cfg.getfloat('fan', 'lv2')
        conf['fan']['lv3'] = cfg.getfloat('fan', 'lv3')
        # key
        conf['key']['click'] = cfg.get('key', 'click')
        conf['key']['twice'] = cfg.get('key', 'twice')
        conf['key']['press'] = cfg.get('key', 'press')
        # time
        conf['time']['twice'] = cfg.getfloat('time', 'twice')
        conf['time']['press'] = cfg.getfloat('time', 'press')
        # other
        conf['slider']['auto'] = cfg.getboolean('slider', 'auto')
        conf['slider']['time'] = cfg.getfloat('slider', 'time')
        conf['oled']['rotate'] = cfg.getboolean('oled', 'rotate')
        conf['oled']['f-temp'] = cfg.getboolean('oled', 'f-temp')
    except Exception:
        traceback.print_exc()
        # fan
        conf['fan']['lv0'] = 35
        conf['fan']['lv1'] = 40
        conf['fan']['lv2'] = 45
        conf['fan']['lv3'] = 50
        # key
        conf['key']['click'] = 'slider'
        conf['key']['twice'] = 'switch'
        conf['key']['press'] = 'none'
        # time
        conf['time']['twice'] = 0.7  # second
        conf['time']['press'] = 1.8
        # other
        conf['slider']['auto'] = True
        conf['slider']['time'] = 10  # second
        conf['oled']['rotate'] = False
        conf['oled']['f-temp'] = False

    return conf


def read_key(pattern, size):
    CHIP_NAME = os.environ['BUTTON_CHIP']
    LINE_NUMBER = int(os.environ['BUTTON_LINE'])

    s = ''
    chip, line = request_input_line(CHIP_NAME, LINE_NUMBER, 'hat_button', pull_up=True)

    try:
        while True:
            s = s[-size:] + str(read_line(line, LINE_NUMBER))
            for t, p in pattern.items():
                if p.match(s):
                    return t
            time.sleep(0.1)
    finally:
        release_line(chip, line)


def watch_key(q=None):
    size = int(conf['time']['press'] * 10)
    wait = int(conf['time']['twice'] * 10)
    pattern = {
        'click': re.compile(r'1+0+1{%d,}' % wait),
        'twice': re.compile(r'1+0+1+0+1{3,}'),
        'press': re.compile(r'1+0{%d,}' % size),
    }

    while True:
        q.put(read_key(pattern, size))


def get_disk_info(cache={}):
    if not cache.get('time') or time.time() - cache['time'] > 30:
        info = {}
        cmd = "df -h | awk '$NF==\"/\"{printf \"%s\", $5}'"
        info['root'] = check_output(cmd)
        for x in conf['disk']:
            cmd = "df -Bg | awk '$1==\"/dev/{}\" {{printf \"%s\", $5}}'".format(x)
            info[x] = check_output(cmd)
        cache['info'] = list(zip(*info.items()))
        cache['time'] = time.time()

    return cache['info']


def slider_next(pages):
    conf['idx'].value += 1
    return pages[conf['idx'].value % len(pages)]


def slider_sleep():
    time.sleep(conf['slider']['time'])


def fan_temp2dc(t):
    for lv, dc in lv2dc.items():
        if t >= conf['fan'][lv]:
            return dc
    return 0.999


def fan_switch():
    conf['run'].value = not conf['run'].value


def get_func(key):
    return conf['key'].get(key, 'none')


def disk_turn_on():
    global sata_lines
    chip_name = os.environ['SATA_CHIP']
    line1 = int(os.environ['SATA_LINE_1'])
    line2 = int(os.environ['SATA_LINE_2'])

    if GPIOD_V2:
        chip = gpiod.Chip(gpiochip_path(chip_name))
        settings = gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT)
        request = chip.request_lines(
            config={(line1, line2): settings},
            consumer='sata_power',
            output_values={
                line1: gpiod.line.Value.ACTIVE,
                line2: gpiod.line.Value.ACTIVE,
            },
        )
        sata_lines = [chip, request]
        return

    chip = gpiod.Chip(str(chip_name))
    line1 = chip.get_line(line1)
    line1.request(consumer='SATA_LINE_1', type=gpiod.LINE_REQ_DIR_OUT)
    line1.set_value(1)
    line2 = chip.get_line(line2)
    line2.request(consumer='SATA_LINE_2', type=gpiod.LINE_REQ_DIR_OUT)
    line2.set_value(1)
    sata_lines = [chip, line1, line2]


conf = {'disk': [], 'idx': mp.Value('d', -1), 'run': mp.Value('d', 1)}
conf.update(read_conf())
