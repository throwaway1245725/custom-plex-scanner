#local media assets agent
import base64
import hashlib
import json
import os
import plistlib
import re
import string
import time
import unicodedata
import urllib

import requests
from dateutil.parser import parse
from mutagen import File
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

import audiohelpers
import config
import helpers
import localmedia
import videohelpers

PERSONAL_MEDIA_IDENTIFIER = "com.plexapp.agents.custom"

GENERIC_ARTIST_NAMES = ['various artists', '[unknown artist]', 'soundtrack', 'ost', 'original sound track', 'original soundtrack', 'original broadway cast']

BLANK_FIELD = '\x7f'

def CleanFilename(path):
  s = os.path.splitext(os.path.basename(path))[0]
  s = CleanString(s)
  s = re.sub(r'^[0-9 \._-]+', '', s)
  return s

def CleanString(s):
  return str(s).strip('\0 ')

def StringOrBlank(s):
  if s is not None:
    s = CleanString(s)
    if len(s) == 0:
      s = BLANK_FIELD
  else:
    s = BLANK_FIELD
  return s

#####################################################################################################################

@expose
def ReadTags(f):
  try:
    return dict(File(f, easy=True))
  except Exception, e:
    Log('Error reading tags from file: %s' % f)
    return {}

#####################################################################################################################

def Start():
    HTTP.ClearCache()
    HTTP.CacheTime = CACHE_1MINUTE * 20
    HTTP.Headers['Accept-Encoding'] = 'gzip'

    requests.packages.urllib3.disable_warnings()

def ValidatePrefs():
    Log('ValidatePrefs function call')

class CustomLocalMediaMovies(Agent.Movies):
  name = 'Custom Local Media Assets (Movies)'
  languages = [Locale.Language.NoLanguage]
  primary_provider = True
  persist_stored_files = True
  accepts_from = None
  contributes_to = None

  title_regex = re.compile(r"^(?P<studio>\w+(?:\.\w+)*)\.(?P<datestr>(?:\d{2}|\d{4})\.\d{2}\.\d{2})\.(?P<actress1>[a-z]+(?:(?!\.and\.)\.[a-z]+)?)(?:\.and\.(?P<actress2>[a-z]+(?:\.[a-z]+)?))?(?:\.(?P<title>[\w\.]+))?\.XXX", re.I)
  actress_photos = {}
  
  def search(self, results, media, lang):
    Log('----------------------------------------search------------------------------------------')
    # Compute the GUID based on the media hash.
    part = media.all_parts()[0]
    
    results.Append(MetadataSearchResult(id=part.hash, name=media.name, lang=lang, score=100))

  def update(self, metadata, media, lang, force):
    Log('----------------------------------------update------------------------------------------')
    # Clear out the title to ensure stale data doesn't clobber other agents' contributions.
    metadata.title = None
    metadata.roles.clear()
    metadata.collections.clear()

    part = media.all_parts()[0]
    path = os.path.dirname(part.file)
    
    # Look for local media.
    try: localmedia.findAssets(metadata, media.title, [path], 'movie', media.all_parts())
    except Exception, e: 
      Log('Error finding media for movie %s: %s' % (media.title, str(e)))

    # Look for subtitles
    for item in media.items:
      for part in item.parts:
        localmedia.findSubtitles(part)

    # If there is an appropriate VideoHelper, use it.
    video_helper = videohelpers.VideoHelpers(part.file)
    if video_helper:
      video_helper.process_metadata(metadata)

    # -------------------------------------- custom stuff starts here -----------------------------------------------
    Log('---------------------------------------parsing title------------------------------------------')
    filename = os.path.basename(media.all_parts()[0].file)
    Log('-------------filename: "{}"'.format(filename))
    m = self.title_regex.match(filename)
    if m: 
      data = m.groupdict()
      Log(data)
      
      metadata.content_rating = 'XXX'

      parsed_date = parse(data['datestr'], yearfirst=True)
      metadata.originally_available_at = parsed_date
      metadata.year = metadata.originally_available_at.year

      metadata.studio = data['studio'].replace('.', ' ')

      def add_actress(name):
        role = metadata.roles.new()
        role.name = name
        role.role = name
        role.photo = get_from_freeones(name)
        return role
      if data['actress1']:
        role = add_actress(data['actress1'].replace('.', ' '))
        metadata.collections.add(role.name)
      if data['actress2']:
        role = add_actress(data['actress2'].replace('.', ' '))
        metadata.collections.add(role.name)
      if not metadata.collections:
        Log('-------------------------------not in any collections!-------------------------------')
        metadata.collections.add('Others')
      
      if data['title']:
        metadata.title = '{studio} - {date} - {actors} - {title}'.format(
          studio=metadata.studio,
          date=metadata.originally_available_at.strftime('%Y/%m/%d'),
          actors=', '.join([role.name for role in metadata.roles]),
          title=data['title'].replace('.', ' ')
        )
      else:
        metadata.title = '{studio} - {date} - {actors}'.format(
          studio=metadata.studio,
          date=metadata.originally_available_at.strftime('%Y/%m/%d'),
          actors=', '.join([role.name for role in metadata.roles])
        )

actor_photo_urls = {}

def get_from_freeones(actor_name):
  if actor_name in actor_photo_urls:
    return actor_photo_urls[actor_name]
  else:
    actor_photo_url = ''
    req = requests.request('GET', 'https://www.freeones.com/babes?q=' + urllib.quote(actor_name))
    actor_search = HTML.ElementFromString(req.text)
    actor_page_url = actor_search.xpath('//div[contains(@class, "grid-item")]//a/@href')
    if actor_page_url:
        actor_page_url = actor_page_url[0].replace('/feed', '/bio', 1)
        actor_page_url = 'https://www.freeones.com' + actor_page_url
        req = requests.request('GET', actor_page_url)
        actor_page = HTML.ElementFromString(req.text)

        db_actor_name = actor_page.xpath('//h1')[0].text_content().lower().replace(' bio', '').strip()
        aliases = actor_page.xpath('//p[contains(., "Aliases")]/following-sibling::div/p')
        if aliases:
            aliases = aliases[0].text_content().strip()
            if aliases:
                aliases = [alias.strip().lower() for alias in aliases.split(',')]
            else:
                aliases = []

        aliases.append(db_actor_name)

        img = actor_page.xpath('//div[contains(@class, "image-container")]//a/img/@src')

        is_true = False
        professions = actor_page.xpath('//p[contains(., "Profession")]/following-sibling::div/p')
        if professions:
            professions = professions[0].text_content().strip()

            for profession in professions.split(','):
                profession = profession.strip()
                if profession in ['Porn Stars', 'Adult Models']:
                    is_true = True
                    break

        if img and actor_name.lower() in aliases and is_true:
            actor_photo_url = img[0]
    actor_photo_urls[actor_name] = actor_photo_url
    return actor_photo_url

