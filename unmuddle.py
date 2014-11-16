#!/usr/bin/env python

import sys
import os
import errno
import re
import datetime
import argparse
import struct
import shutil
import subprocess
import xml.etree.ElementTree as ET

from stf2pdf import STF2PDF

time_offset = 0 # The offset between pen time and system time

encoder = None # The command for the audio transcoder
thumbnailer = None # The command for the thumbnail generator
merger = None # The command for the pdf merger
pages = {} # Will be populated with page info dicts indexed by page address
notebooks = {} # Will be populated with arrays of pdfs indexed by notebook id

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Convert file formats and organize output from dumpscribe."
)
parser.add_argument('--aac', dest='keep_aac', action='store_true',
                    help="Don't convert aac audio files to ogg vorbis. (conversion required either ffmpeg or avconv)")
parser.add_argument('--notebook', dest='notebook', action='store_true',
                    help="Additionally create one pdf per notebook with all notebook pages (requires pdftk).")
parser.add_argument('--thumb', dest='gen_thumbnails', action='store_true',
                    help="Generate png thumbnails of pdfs (requires either ImageMagick or GraphicsMagick).")
parser.add_argument('--thumbsize', dest='thumbsize', type=int,
                    help="Set thumbnail maximum dimension.")
parser.add_argument('input_dir', nargs=1, 
                    help="The directory generated by dumpscribe.")
parser.add_argument('output_dir', nargs=1, 
                    help="Where to write the output from this program.")

args = parser.parse_args()

thumbnail_size = args.thumbsize or 300
indir = args.input_dir[0]
outdir = args.output_dir[0]

# detect if pdftk is present on the system
def detect_merger():
    ret = subprocess.call("pdftk --version > /dev/null 2>&1", shell=True)
    if ret == 0:
        return "pdftk"
    return None

# detect if ImageMagick convert or GraphicsMagick convert are present on the system
def detect_thumbnailer():
    ret = subprocess.call("convert --version > /dev/null 2>&1", shell=True)
    if ret == 0:
        return "convert"
    ret = subprocess.call("gm convert -help > /dev/null 2>&1", shell=True)
    if ret == 0:
        return "gm convert"
    return None

# detect if ffmpeg or avconv is present on the system
def detect_encoder():
    ret = subprocess.call("ffmpeg -h > /dev/null 2>&1", shell=True)
    if ret == 0:
        return "ffmpeg"
    ret = subprocess.call("avconv -h > /dev/null 2>&1", shell=True)
    if ret == 0:
        return "avconv"

    return None

if not args.keep_aac:
    encoder = detect_encoder()
    if not encoder:
        sys.exit("No encoder found. Please install either avconv or ffmpeg or run this command with the --aac flag.")

if args.gen_thumbnails:
    thumbnailer = detect_thumbnailer()
    if not thumbnailer:
        sys.exit("No thumbnail generator found. Please install either ImageMagick or GraphicsMagick or run this command without the --thumb flag.")
    
if args.notebook:
    merger = detect_merger()
    if not merger:
        sys.exit("No pdf merger found. Please install pdftk or run this command without the --notebook flag.")

# get audio duration in seconds 
def get_audio_duration(in_file):
    cmd = '%s -i "%s" 2>&1; echo "success"' % (encoder, in_file)
    out = subprocess.check_output(cmd, shell=True)
    m = re.search("Duration:\s+([\d:]+)", out)
    if not m:
        return None

    try:
        duration_str = m.group(1)
        parts = duration_str.split(":")
        duration = int(parts[0]) * 60 * 60 + int(parts[1]) * 60 + int(parts[2])
    except:
        print "duration failed :("
        return None

    return duration

def convert_aac_to_ogg(in_file, out_file):
    sys.stdout.write("Transcoding audio file... ")
    cmd = '%s -i "%s" -acodec libvorbis "%s" > /dev/null 2>&1' % (encoder, in_file, out_file)
    ret = subprocess.call(cmd, shell=True)
    print "done."

def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else: raise

def pentime_to_unixtime(pentime):
    return (int(pentime) + time_offset) / 1000

def gen_thumbnail(pdf_filepath):
    dirname = os.path.join(os.path.dirname(pdf_filepath), 'thumbnails')
    mkdir_p(dirname)
    filename = os.path.basename(pdf_filepath) + '.png'
    png_filepath = os.path.join(dirname, filename)

    sys.stdout.write("Generating thumbnail... ")
    cmd = '%s -resize %s %s %s' % (thumbnailer, thumbnail_size, pdf_filepath, png_filepath)
    subprocess.call(cmd, shell=True)
    print "done."
    

# merge a set of pages into a single notebook pdf
def gen_notebook(pdf_filepaths):
    if not pdf_filepaths or (len(pdf_filepaths) < 1):
        return

    sys.stdout.write("Generating notebook pdf... ")
    filepaths = '"' + '" "'.join(pdf_filepaths) + '"'
    output_file = os.path.join(os.path.dirname(pdf_filepaths[0]), 'all_pages.pdf')

    cmd = '%s %s cat output "%s"' % (merger, filepaths, output_file)
    subprocess.call(cmd, shell=True)
    print "done."


def copy_and_convert_stf(page, stf_path, dest):
    mkdir_p(os.path.dirname(dest))
    f = open(stf_path)

    if page['number'] % 2 > 0:
        bgfile = 'right.png'
    else:
        bgfile = 'left.png'

    sys.stdout.write("Converting stf to pdf... ")
    STF2PDF(f).convert(dest, os.path.join('backgrounds', bgfile))    
    print "done."
    os.utime(dest, (page['time'], page['time']))
    f.close()

    if args.gen_thumbnails:
        gen_thumbnail(dest)

def copy_audio(audio_file):
    
    info_file = os.path.join(os.path.dirname(audio_file), 'session.info')
    f = open(info_file)
    f.read(16)
    time_raw = f.read(8)
    timestamp = struct.unpack(">Q", time_raw)[0]
    print "pre " + str(timestamp)
    timestamp = pentime_to_unixtime(timestamp)
    print "post " + str(timestamp)
    time = datetime.datetime.fromtimestamp(timestamp)
    timestr =  time.strftime('%Y-%m-%d_%H:%M')
    print timestr
    f.close()

    # Attempt to get page address of first associated page (if any)
    # Warning: This is parsing a reverse-engineered binary format
    #          If my assumptions and guesses are incorrect this may fail
    #          and recording will not be associated with their pages/notebooks.
    # See NOTES.md for more info.
    page = None
    page_address = None
    pages_file = os.path.join(os.path.dirname(audio_file), 'session.pages')
    try:
        f = open(pages_file, 'rb')
        f.read(6)
        p1_raw = '\x00' + f.read(3)
        p1 = struct.unpack(">I", p1_raw)[0]
        
        p2_raw = f.read(2)
        p2 = struct.unpack(">H", p2_raw)[0]
        
        p3_raw = struct.unpack(">B", f.read(1))[0]
        shared = struct.unpack(">B", f.read(1))[0] # half this byte belongs to p3, half to p4

        p3 = (p3_raw << 4) | (shared >> 4)

        p4_raw = struct.unpack(">B", f.read(1))[0]
        p4 = ((shared & 15) << 8) | p4_raw

        page_address = str(p1) + '.' + str(p2) + '.' + str(p3) + '.' + str(p4)
        page = pages[page_address]

    except:
        page_address = None

    duration = get_audio_duration(audio_file);
    if not duration:
        duration = 0
    filepost = "recording_" + timestr + "_" + str(duration)

    if page:
        dest_dir = os.path.join(outdir, 'notebook-' + page['notebook'])
        mkdir_p(dest_dir)
        dest = os.path.join(dest_dir, 'page-' + str(page['number']) + "-" + filepost)

    else:
        dest_dir = os.path.join(outdir, 'other_recordings')
        mkdir_p(dest_dir)
        dest = os.path.join(dest_dir, filepost)

    if args.keep_aac:
        dest += '.aac'
        shutil.copyfile(audio_file, dest)
    else:
        dest += '.ogg'
        convert_aac_to_ogg(audio_file, dest)

    os.utime(dest, (timestamp, timestamp))

def sort_pdf_paths_by_page_number(a, b):
    regex ="(\d+)\.pdf$"
    x = 0
    y = 0
    m = re.search(regex, a)
    if m:
        x = int(m.group(1))
    m = re.search(regex, b)
    if m:
        y = int(m.group(1))

    return x - y

### function definitions above this line ###



# Get difference between pen time and system time.
# This is important since pen time is relative to some
# weird non-standard and unknown reference point.
# It is not just milliseconds from January 1st 1970 :(
offsetFile = open(os.path.join(indir, "time_offset"), "r")
time_offset = int(offsetFile.read())
offsetFile.close()

# Parse page metadata from xml into a dict
sys.stdout.write("Parsing page list... ")
xml_root = ET.parse(os.path.join(indir, "written_page_list.xml"))
for lsp in xml_root.findall('.//lsp'):
    notebook = lsp.attrib.get('guid')
    for page in lsp.findall('page'):
        address = page.attrib.get('pageaddress')
        if not address:
            continue
        # build the pages dict
        pages[address] = {
            'notebook': notebook,
            'number': int(page.attrib.get('page')),
            'time': pentime_to_unixtime(page.attrib.get('end_time'))
        }
print "done."

# find stf files
for root, dirs, files in os.walk(os.path.join(indir, "data")):
    path = root.split('/')

    page_address = None
    for el in path:
        res = re.match("\d+\.\d+\.\d+\.\d+", el)
        if res:
          page_address = res.group(0)

    for file in files:
        res = re.match(".*\.stf$", file)
        if not res or not page_address:
            continue

        page = pages[page_address]

        time = datetime.datetime.fromtimestamp(page['time'])
        timestr = time.strftime('%Y-%m-%d_%H:%M')
        outfile = os.path.join(outdir, 'notebook-' + page['notebook'], 'page-' + str(page['number']) + '.pdf')
        copy_and_convert_stf(page, os.path.join(root, file), outfile)

        # build the notebooks dict
        if not page['notebook'] in notebooks:
            notebooks[page['notebook']] = [outfile]
        else:
            notebooks[page['notebook']].append(outfile)


# generate full notebook pdfs if needed
if args.notebook:
    for key in notebooks:
        # sort pdfs in notebooks dict by page number
        notebooks[key].sort(sort_pdf_paths_by_page_number)
        gen_notebook(notebooks[key])

# find audio files
for root, dirs, files in os.walk(os.path.join(indir, "userdata")):
    path = root.split('/')
    page_address = None

    audio_id = None
    for el in path:
        res = re.match("[abcdef\d]{16}", el);
        if res:
            audio_id = res.group(0)

    for file in files:
        res = re.match(".*\.aac$", file)
        if not res:
            continue

        copy_audio(os.path.join(root, file))

print "Unmuddle completed successfully."
