"""Support to interface with Sonos players."""
import datetime
import functools as ft
import logging
import socket
import asyncio
import urllib

import async_timeout
import requests
import voluptuous as vol

from homeassistant.components.media_player import (
    MediaPlayerDevice, PLATFORM_SCHEMA)
from homeassistant.components.media_player.const import (
    ATTR_MEDIA_ENQUEUE, DOMAIN, MEDIA_TYPE_MUSIC,
    SUPPORT_CLEAR_PLAYLIST, SUPPORT_NEXT_TRACK, SUPPORT_PAUSE, SUPPORT_PLAY,
    SUPPORT_PLAY_MEDIA, SUPPORT_PREVIOUS_TRACK, SUPPORT_SEEK,
    SUPPORT_SELECT_SOURCE, SUPPORT_SHUFFLE_SET, SUPPORT_STOP,
    SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET)
from homeassistant.components.sonos import DOMAIN as SONOS_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID, ATTR_TIME, CONF_HOSTS, STATE_IDLE, STATE_OFF, STATE_PAUSED,
    STATE_PLAYING)
import homeassistant.helpers.config_validation as cv
from homeassistant.util.dt import utcnow

DEPENDENCIES = ('sonos',)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Quiet down pysonos logging to just actual problems.
logging.getLogger('pysonos').setLevel(logging.WARNING)
logging.getLogger('pysonos.data_structures_entry').setLevel(logging.ERROR)

SUPPORT_SONOS = SUPPORT_VOLUME_SET | SUPPORT_VOLUME_MUTE |\
    SUPPORT_PLAY | SUPPORT_PAUSE | SUPPORT_STOP | SUPPORT_SELECT_SOURCE |\
    SUPPORT_PREVIOUS_TRACK | SUPPORT_NEXT_TRACK | SUPPORT_SEEK |\
    SUPPORT_PLAY_MEDIA | SUPPORT_SHUFFLE_SET | SUPPORT_CLEAR_PLAYLIST

SERVICE_JOIN = 'sonos_join'
SERVICE_UNJOIN = 'sonos_unjoin'
SERVICE_SNAPSHOT = 'sonos_snapshot'
SERVICE_RESTORE = 'sonos_restore'
SERVICE_SET_TIMER = 'sonos_set_sleep_timer'
SERVICE_CLEAR_TIMER = 'sonos_clear_sleep_timer'
SERVICE_UPDATE_ALARM = 'sonos_update_alarm'
SERVICE_SET_OPTION = 'sonos_set_option'

DATA_SONOS = 'sonos_media_player'

SOURCE_LINEIN = 'Line-in'
SOURCE_TV = 'TV'

CONF_ADVERTISE_ADDR = 'advertise_addr'
CONF_INTERFACE_ADDR = 'interface_addr'

# Service call validation schemas
ATTR_SLEEP_TIME = 'sleep_time'
ATTR_ALARM_ID = 'alarm_id'
ATTR_VOLUME = 'volume'
ATTR_ENABLED = 'enabled'
ATTR_INCLUDE_LINKED_ZONES = 'include_linked_zones'
ATTR_MASTER = 'master'
ATTR_WITH_GROUP = 'with_group'
ATTR_NIGHT_SOUND = 'night_sound'
ATTR_SPEECH_ENHANCE = 'speech_enhance'

ATTR_SONOS_GROUP = 'sonos_group'

UPNP_ERRORS_TO_IGNORE = ['701', '711', '712']

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_ADVERTISE_ADDR): cv.string,
    vol.Optional(CONF_INTERFACE_ADDR): cv.string,
    vol.Optional(CONF_HOSTS): vol.All(cv.ensure_list, [cv.string]),
})

SONOS_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
})

SONOS_JOIN_SCHEMA = SONOS_SCHEMA.extend({
    vol.Required(ATTR_MASTER): cv.entity_id,
})

SONOS_STATES_SCHEMA = SONOS_SCHEMA.extend({
    vol.Optional(ATTR_WITH_GROUP, default=True): cv.boolean,
})

SONOS_SET_TIMER_SCHEMA = SONOS_SCHEMA.extend({
    vol.Required(ATTR_SLEEP_TIME):
        vol.All(vol.Coerce(int), vol.Range(min=0, max=86399))
})

SONOS_UPDATE_ALARM_SCHEMA = SONOS_SCHEMA.extend({
    vol.Required(ATTR_ALARM_ID): cv.positive_int,
    vol.Optional(ATTR_TIME): cv.time,
    vol.Optional(ATTR_VOLUME): cv.small_float,
    vol.Optional(ATTR_ENABLED): cv.boolean,
    vol.Optional(ATTR_INCLUDE_LINKED_ZONES): cv.boolean,
})

SONOS_SET_OPTION_SCHEMA = SONOS_SCHEMA.extend({
    vol.Optional(ATTR_NIGHT_SOUND): cv.boolean,
    vol.Optional(ATTR_SPEECH_ENHANCE): cv.boolean,
})


class SonosData:
    """Storage class for platform global data."""

    def __init__(self, hass):
        """Initialize the data."""
        self.uids = set()
        self.entities = []
        self.topology_condition = asyncio.Condition(loop=hass.loop)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Sonos platform.

    Deprecated.
    """
    _LOGGER.warning('Loading Sonos via platform config is deprecated.')
    _setup_platform(hass, config, add_entities, discovery_info)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Sonos from a config entry."""
    def add_entities(entities, update_before_add=False):
        """Sync version of async add entities."""
        hass.add_job(async_add_entities, entities, update_before_add)

    hass.async_add_executor_job(
        _setup_platform, hass, hass.data[SONOS_DOMAIN].get('media_player', {}),
        add_entities, None)


def _setup_platform(hass, config, add_entities, discovery_info):
    """Set up the Sonos platform."""
    import pysonos

    if DATA_SONOS not in hass.data:
        hass.data[DATA_SONOS] = SonosData(hass)

    advertise_addr = config.get(CONF_ADVERTISE_ADDR)
    if advertise_addr:
        pysonos.config.EVENT_ADVERTISE_IP = advertise_addr

    players = []
    if discovery_info:
        player = pysonos.SoCo(discovery_info.get('host'))

        # If host already exists by config
        if player.uid in hass.data[DATA_SONOS].uids:
            return

        # If invisible, such as a stereo slave
        if not player.is_visible:
            return

        players.append(player)
    else:
        hosts = config.get(CONF_HOSTS)
        if hosts:
            # Support retro compatibility with comma separated list of hosts
            # from config
            hosts = hosts[0] if len(hosts) == 1 else hosts
            hosts = hosts.split(',') if isinstance(hosts, str) else hosts
            for host in hosts:
                try:
                    players.append(pysonos.SoCo(socket.gethostbyname(host)))
                except OSError:
                    _LOGGER.warning("Failed to initialize '%s'", host)
        else:
            players = pysonos.discover(
                interface_addr=config.get(CONF_INTERFACE_ADDR),
                all_households=True)

        if not players:
            _LOGGER.warning("No Sonos speakers found")
            return

    hass.data[DATA_SONOS].uids.update(p.uid for p in players)
    add_entities(SonosEntity(p) for p in players)
    _LOGGER.debug("Added %s Sonos speakers", len(players))

    def _service_to_entities(service):
        """Extract and return entities from service call."""
        entity_ids = service.data.get('entity_id')

        entities = hass.data[DATA_SONOS].entities
        if entity_ids:
            entities = [e for e in entities if e.entity_id in entity_ids]

        return entities

    async def async_service_handle(service):
        """Handle async services."""
        entities = _service_to_entities(service)

        if service.service == SERVICE_JOIN:
            master = [e for e in hass.data[DATA_SONOS].entities
                      if e.entity_id == service.data[ATTR_MASTER]]
            if master:
                await SonosEntity.join_multi(hass, master[0], entities)
        elif service.service == SERVICE_UNJOIN:
            await SonosEntity.unjoin_multi(hass, entities)
        elif service.service == SERVICE_SNAPSHOT:
            await SonosEntity.snapshot_multi(
                hass, entities, service.data[ATTR_WITH_GROUP])
        elif service.service == SERVICE_RESTORE:
            await SonosEntity.restore_multi(
                hass, entities, service.data[ATTR_WITH_GROUP])

    hass.services.register(
        DOMAIN, SERVICE_JOIN, async_service_handle,
        schema=SONOS_JOIN_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_UNJOIN, async_service_handle,
        schema=SONOS_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_SNAPSHOT, async_service_handle,
        schema=SONOS_STATES_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_RESTORE, async_service_handle,
        schema=SONOS_STATES_SCHEMA)

    def service_handle(service):
        """Handle sync services."""
        for entity in _service_to_entities(service):
            if service.service == SERVICE_SET_TIMER:
                entity.set_sleep_timer(service.data[ATTR_SLEEP_TIME])
            elif service.service == SERVICE_CLEAR_TIMER:
                entity.clear_sleep_timer()
            elif service.service == SERVICE_UPDATE_ALARM:
                entity.set_alarm(**service.data)
            elif service.service == SERVICE_SET_OPTION:
                entity.set_option(**service.data)

    hass.services.register(
        DOMAIN, SERVICE_SET_TIMER, service_handle,
        schema=SONOS_SET_TIMER_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_CLEAR_TIMER, service_handle,
        schema=SONOS_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_UPDATE_ALARM, service_handle,
        schema=SONOS_UPDATE_ALARM_SCHEMA)

    hass.services.register(
        DOMAIN, SERVICE_SET_OPTION, service_handle,
        schema=SONOS_SET_OPTION_SCHEMA)


class _ProcessSonosEventQueue:
    """Queue like object for dispatching sonos events."""

    def __init__(self, handler):
        """Initialize Sonos event queue."""
        self._handler = handler

    def put(self, item, block=True, timeout=None):
        """Process event."""
        self._handler(item)


def _get_entity_from_soco_uid(hass, uid):
    """Return SonosEntity from SoCo uid."""
    for entity in hass.data[DATA_SONOS].entities:
        if uid == entity.unique_id:
            return entity
    return None


def soco_error(errorcodes=None):
    """Filter out specified UPnP errors from logs and avoid exceptions."""
    def decorator(funct):
        """Decorate functions."""
        @ft.wraps(funct)
        def wrapper(*args, **kwargs):
            """Wrap for all soco UPnP exception."""
            from pysonos.exceptions import SoCoUPnPException, SoCoException

            try:
                return funct(*args, **kwargs)
            except SoCoUPnPException as err:
                if errorcodes and err.error_code in errorcodes:
                    pass
                else:
                    _LOGGER.error("Error on %s with %s", funct.__name__, err)
            except SoCoException as err:
                _LOGGER.error("Error on %s with %s", funct.__name__, err)

        return wrapper
    return decorator


def soco_coordinator(funct):
    """Call function on coordinator."""
    @ft.wraps(funct)
    def wrapper(entity, *args, **kwargs):
        """Wrap for call to coordinator."""
        if entity.is_coordinator:
            return funct(entity, *args, **kwargs)
        return funct(entity.coordinator, *args, **kwargs)

    return wrapper


def _timespan_secs(timespan):
    """Parse a time-span into number of seconds."""
    if timespan in ('', 'NOT_IMPLEMENTED', None):
        return None

    return sum(60 ** x[0] * int(x[1]) for x in enumerate(
        reversed(timespan.split(':'))))


def _is_radio_uri(uri):
    """Return whether the URI is a radio stream."""
    radio_schemes = (
        'x-rincon-mp3radio:', 'x-sonosapi-stream:', 'x-sonosapi-radio:',
        'x-sonosapi-hls:', 'hls-radio:')
    return uri.startswith(radio_schemes)


class SonosEntity(MediaPlayerDevice):
    """Representation of a Sonos entity."""

    def __init__(self, player):
        """Initialize the Sonos entity."""
        self._subscriptions = []
        self._receives_events = False
        self._volume_increment = 2
        self._unique_id = player.uid
        self._player = player
        self._model = None
        self._player_volume = None
        self._player_muted = None
        self._shuffle = None
        self._name = None
        self._coordinator = None
        self._sonos_group = [self]
        self._status = None
        self._media_duration = None
        self._media_position = None
        self._media_position_updated_at = None
        self._media_image_url = None
        self._media_artist = None
        self._media_album_name = None
        self._media_title = None
        self._night_sound = None
        self._speech_enhance = None
        self._source_name = None
        self._available = True
        self._favorites = None
        self._soco_snapshot = None
        self._snapshot_group = None

        self._set_basic_information()

    async def async_added_to_hass(self):
        """Subscribe sonos events."""
        self.hass.data[DATA_SONOS].entities.append(self)
        self.hass.async_add_executor_job(self._subscribe_to_player_events)

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    def __hash__(self):
        """Return a hash of self."""
        return hash(self.unique_id)

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def device_info(self):
        """Return information about the device."""
        return {
            'identifiers': {
                (SONOS_DOMAIN, self._unique_id)
            },
            'name': self._name,
            'model': self._model.replace("Sonos ", ""),
            'manufacturer': 'Sonos',
        }

    @property
    @soco_coordinator
    def state(self):
        """Return the state of the entity."""
        if self._status in ('PAUSED_PLAYBACK', 'STOPPED'):
            return STATE_PAUSED
        if self._status in ('PLAYING', 'TRANSITIONING'):
            return STATE_PLAYING
        if self._status == 'OFF':
            return STATE_OFF
        return STATE_IDLE

    @property
    def is_coordinator(self):
        """Return true if player is a coordinator."""
        return self._coordinator is None

    @property
    def soco(self):
        """Return soco object."""
        return self._player

    @property
    def coordinator(self):
        """Return coordinator of this player."""
        return self._coordinator

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    def _check_available(self):
        """Check that we can still connect to the player."""
        try:
            sock = socket.create_connection(
                address=(self.soco.ip_address, 1443), timeout=3)
            sock.close()
            return True
        except socket.error:
            return False

    def _set_basic_information(self):
        """Set initial entity information."""
        speaker_info = self.soco.get_speaker_info(True)
        self._name = speaker_info['zone_name']
        self._model = speaker_info['model_name']
        self._shuffle = self.soco.shuffle

        self.update_volume()

        self._set_favorites()

    def _set_favorites(self):
        """Set available favorites."""
        # SoCo 0.16 raises a generic Exception on invalid xml in favorites.
        # Filter those out now so our list is safe to use.
        try:
            self._favorites = []
            for fav in self.soco.music_library.get_sonos_favorites():
                try:
                    if fav.reference.get_uri():
                        self._favorites.append(fav)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("Ignoring invalid favorite '%s'", fav.title)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Ignoring invalid favorite list")

    def _radio_artwork(self, url):
        """Return the private URL with artwork for a radio stream."""
        if url not in ('', 'NOT_IMPLEMENTED', None):
            if url.find('tts_proxy') > 0:
                # If the content is a tts don't try to fetch an image from it.
                return None
            url = 'http://{host}:{port}/getaa?s=1&u={uri}'.format(
                host=self.soco.ip_address,
                port=1400,
                uri=urllib.parse.quote(url, safe='')
            )
        return url

    def _subscribe_to_player_events(self):
        """Add event subscriptions."""
        self._receives_events = False

        # New player available, build the current group topology
        for entity in self.hass.data[DATA_SONOS].entities:
            entity.update_groups()

        player = self.soco

        def subscribe(service, action):
            """Add a subscription to a pysonos service."""
            queue = _ProcessSonosEventQueue(action)
            sub = service.subscribe(auto_renew=True, event_queue=queue)
            self._subscriptions.append(sub)

        subscribe(player.avTransport, self.update_media)
        subscribe(player.renderingControl, self.update_volume)
        subscribe(player.zoneGroupTopology, self.update_groups)
        subscribe(player.contentDirectory, self.update_content)

    def update(self):
        """Retrieve latest state."""
        available = self._check_available()
        if self._available != available:
            self._available = available
            if available:
                self._set_basic_information()
                self._subscribe_to_player_events()
            else:
                for subscription in self._subscriptions:
                    self.hass.async_add_executor_job(subscription.unsubscribe)
                self._subscriptions = []

                self._player_volume = None
                self._player_muted = None
                self._status = 'OFF'
                self._coordinator = None
                self._media_duration = None
                self._media_position = None
                self._media_position_updated_at = None
                self._media_image_url = None
                self._media_artist = None
                self._media_album_name = None
                self._media_title = None
                self._source_name = None
        elif available and not self._receives_events:
            self.update_groups()
            self.update_volume()
            if self.is_coordinator:
                self.update_media()

    def update_media(self, event=None):
        """Update information about currently playing media."""
        transport_info = self.soco.get_current_transport_info()
        new_status = transport_info.get('current_transport_state')

        # Ignore transitions, we should get the target state soon
        if new_status == 'TRANSITIONING':
            return

        self._shuffle = self.soco.shuffle

        if self.soco.is_playing_tv:
            self.update_media_linein(SOURCE_TV)
        elif self.soco.is_playing_line_in:
            self.update_media_linein(SOURCE_LINEIN)
        else:
            track_info = self.soco.get_current_track_info()

            if _is_radio_uri(track_info['uri']):
                variables = event and event.variables
                self.update_media_radio(variables, track_info)
            else:
                update_position = (new_status != self._status)
                self.update_media_music(update_position, track_info)

        self._status = new_status

        self.schedule_update_ha_state()

        # Also update slaves
        for entity in self.hass.data[DATA_SONOS].entities:
            coordinator = entity.coordinator
            if coordinator and coordinator.unique_id == self.unique_id:
                entity.schedule_update_ha_state()

    def update_media_linein(self, source):
        """Update state when playing from line-in/tv."""
        self._media_duration = None
        self._media_position = None
        self._media_position_updated_at = None

        self._media_image_url = None

        self._media_artist = source
        self._media_album_name = None
        self._media_title = None

        self._source_name = source

    def update_media_radio(self, variables, track_info):
        """Update state when streaming radio."""
        self._media_duration = None
        self._media_position = None
        self._media_position_updated_at = None

        media_info = self.soco.avTransport.GetMediaInfo([('InstanceID', 0)])
        self._media_image_url = self._radio_artwork(media_info['CurrentURI'])

        self._media_artist = track_info.get('artist')
        self._media_album_name = None
        self._media_title = track_info.get('title')

        if self._media_artist and self._media_title:
            # artist and album name are in the data, concatenate
            # that do display as artist.
            # "Information" field in the sonos pc app
            self._media_artist = '{artist} - {title}'.format(
                artist=self._media_artist,
                title=self._media_title
            )
        elif variables:
            # "On Now" field in the sonos pc app
            current_track_metadata = variables.get('current_track_meta_data')
            if current_track_metadata:
                self._media_artist = \
                    current_track_metadata.radio_show.split(',')[0]

        # For radio streams we set the radio station name as the title.
        current_uri_metadata = media_info["CurrentURIMetaData"]
        if current_uri_metadata not in ('', 'NOT_IMPLEMENTED', None):
            # currently soco does not have an API for this
            import pysonos
            current_uri_metadata = pysonos.xml.XML.fromstring(
                pysonos.utils.really_utf8(current_uri_metadata))

            md_title = current_uri_metadata.findtext(
                './/{http://purl.org/dc/elements/1.1/}title')

            if md_title not in ('', 'NOT_IMPLEMENTED', None):
                self._media_title = md_title

        if self._media_artist and self._media_title:
            # some radio stations put their name into the artist
            # name, e.g.:
            #   media_title = "Station"
            #   media_artist = "Station - Artist - Title"
            # detect this case and trim from the front of
            # media_artist for cosmetics
            trim = '{title} - '.format(title=self._media_title)
            chars = min(len(self._media_artist), len(trim))

            if self._media_artist[:chars].upper() == trim[:chars].upper():
                self._media_artist = self._media_artist[chars:]

        # Check if currently playing radio station is in favorites
        self._source_name = None
        for fav in self._favorites:
            if fav.reference.get_uri() == media_info['CurrentURI']:
                self._source_name = fav.title

    def update_media_music(self, update_media_position, track_info):
        """Update state when playing music tracks."""
        self._media_duration = _timespan_secs(track_info.get('duration'))

        position_info = self.soco.avTransport.GetPositionInfo(
            [('InstanceID', 0),
             ('Channel', 'Master')]
        )
        rel_time = _timespan_secs(position_info.get("RelTime"))

        # player no longer reports position?
        update_media_position |= rel_time is None and \
            self._media_position is not None

        # player started reporting position?
        update_media_position |= rel_time is not None and \
            self._media_position is None

        # position jumped?
        if rel_time is not None and self._media_position is not None:
            time_diff = utcnow() - self._media_position_updated_at
            time_diff = time_diff.total_seconds()

            calculated_position = self._media_position + time_diff

            update_media_position |= abs(calculated_position - rel_time) > 1.5

        if update_media_position:
            self._media_position = rel_time
            self._media_position_updated_at = utcnow()

        self._media_image_url = track_info.get('album_art')

        self._media_artist = track_info.get('artist')
        self._media_album_name = track_info.get('album')
        self._media_title = track_info.get('title')

        self._source_name = None

    def update_volume(self, event=None):
        """Update information about currently volume settings."""
        if event:
            variables = event.variables

            if 'volume' in variables:
                self._player_volume = int(variables['volume']['Master'])

            if 'mute' in variables:
                self._player_muted = (variables['mute']['Master'] == '1')

            if 'night_mode' in variables:
                self._night_sound = (variables['night_mode'] == '1')

            if 'dialog_level' in variables:
                self._speech_enhance = (variables['dialog_level'] == '1')

            self.schedule_update_ha_state()
        else:
            self._player_volume = self.soco.volume
            self._player_muted = self.soco.mute
            self._night_sound = self.soco.night_mode
            self._speech_enhance = self.soco.dialog_mode

    def update_groups(self, event=None):
        """Handle callback for topology change event."""
        def _get_soco_group():
            """Ask SoCo cache for existing topology."""
            coordinator_uid = self.unique_id
            slave_uids = []

            try:
                if self.soco.group and self.soco.group.coordinator:
                    coordinator_uid = self.soco.group.coordinator.uid
                    slave_uids = [p.uid for p in self.soco.group.members
                                  if p.uid != coordinator_uid]
            except requests.exceptions.RequestException:
                pass

            return [coordinator_uid] + slave_uids

        async def _async_extract_group(event):
            """Extract group layout from a topology event."""
            group = event and event.zone_player_uui_ds_in_group
            if group:
                return group.split(',')

            return await self.hass.async_add_executor_job(_get_soco_group)

        def _async_regroup(group):
            """Rebuild internal group layout."""
            sonos_group = []
            for uid in group:
                entity = _get_entity_from_soco_uid(self.hass, uid)
                if entity:
                    sonos_group.append(entity)

            self._coordinator = None
            self._sonos_group = sonos_group
            self.async_schedule_update_ha_state()

            for slave_uid in group[1:]:
                slave = _get_entity_from_soco_uid(self.hass, slave_uid)
                if slave:
                    # pylint: disable=protected-access
                    slave._coordinator = self
                    slave._sonos_group = sonos_group
                    slave.async_schedule_update_ha_state()

        async def _async_handle_group_event(event):
            """Get async lock and handle event."""
            async with self.hass.data[DATA_SONOS].topology_condition:
                group = await _async_extract_group(event)

                if self.unique_id == group[0]:
                    _async_regroup(group)

                    self.hass.data[DATA_SONOS].topology_condition.notify_all()

        if event:
            self._receives_events = True

            if not hasattr(event, 'zone_player_uui_ds_in_group'):
                return

        self.hass.add_job(_async_handle_group_event(event))

    def update_content(self, event=None):
        """Update information about available content."""
        self._set_favorites()
        self.schedule_update_ha_state()

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._player_volume / 100

    @property
    def is_volume_muted(self):
        """Return true if volume is muted."""
        return self._player_muted

    @property
    @soco_coordinator
    def shuffle(self):
        """Shuffling state."""
        return self._shuffle

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MEDIA_TYPE_MUSIC

    @property
    @soco_coordinator
    def media_duration(self):
        """Duration of current playing media in seconds."""
        return self._media_duration

    @property
    @soco_coordinator
    def media_position(self):
        """Position of current playing media in seconds."""
        return self._media_position

    @property
    @soco_coordinator
    def media_position_updated_at(self):
        """When was the position of the current playing media valid."""
        return self._media_position_updated_at

    @property
    @soco_coordinator
    def media_image_url(self):
        """Image url of current playing media."""
        return self._media_image_url or None

    @property
    @soco_coordinator
    def media_artist(self):
        """Artist of current playing media, music track only."""
        return self._media_artist

    @property
    @soco_coordinator
    def media_album_name(self):
        """Album name of current playing media, music track only."""
        return self._media_album_name

    @property
    @soco_coordinator
    def media_title(self):
        """Title of current playing media."""
        return self._media_title

    @property
    @soco_coordinator
    def source(self):
        """Name of the current input source."""
        return self._source_name

    @property
    @soco_coordinator
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_SONOS

    @soco_error()
    def volume_up(self):
        """Volume up media player."""
        self._player.volume += self._volume_increment

    @soco_error()
    def volume_down(self):
        """Volume down media player."""
        self._player.volume -= self._volume_increment

    @soco_error()
    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        self.soco.volume = str(int(volume * 100))

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def set_shuffle(self, shuffle):
        """Enable/Disable shuffle mode."""
        self.soco.shuffle = shuffle

    @soco_error()
    def mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        self.soco.mute = mute

    @soco_error()
    @soco_coordinator
    def select_source(self, source):
        """Select input source."""
        if source == SOURCE_LINEIN:
            self.soco.switch_to_line_in()
        elif source == SOURCE_TV:
            self.soco.switch_to_tv()
        else:
            fav = [fav for fav in self._favorites
                   if fav.title == source]
            if len(fav) == 1:
                src = fav.pop()
                uri = src.reference.get_uri()
                if _is_radio_uri(uri):
                    self.soco.play_uri(uri, title=source)
                else:
                    self.soco.clear_queue()
                    self.soco.add_to_queue(src.reference)
                    self.soco.play_from_queue(0)

    @property
    @soco_coordinator
    def source_list(self):
        """List of available input sources."""
        sources = [fav.title for fav in self._favorites]

        model = self._model.upper()
        if 'PLAY:5' in model or 'CONNECT' in model:
            sources += [SOURCE_LINEIN]
        elif 'PLAYBAR' in model:
            sources += [SOURCE_LINEIN, SOURCE_TV]
        elif 'BEAM' in model:
            sources += [SOURCE_TV]

        return sources

    @soco_error()
    def turn_on(self):
        """Turn the media player on."""
        self.media_play()

    @soco_error()
    def turn_off(self):
        """Turn off media player."""
        self.media_stop()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_play(self):
        """Send play command."""
        self.soco.play()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_stop(self):
        """Send stop command."""
        self.soco.stop()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_pause(self):
        """Send pause command."""
        self.soco.pause()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_next_track(self):
        """Send next track command."""
        self.soco.next()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_previous_track(self):
        """Send next track command."""
        self.soco.previous()

    @soco_error(UPNP_ERRORS_TO_IGNORE)
    @soco_coordinator
    def media_seek(self, position):
        """Send seek command."""
        self.soco.seek(str(datetime.timedelta(seconds=int(position))))

    @soco_error()
    @soco_coordinator
    def clear_playlist(self):
        """Clear players playlist."""
        self.soco.clear_queue()

    @soco_error()
    @soco_coordinator
    def play_media(self, media_type, media_id, **kwargs):
        """
        Send the play_media command to the media player.

        If ATTR_MEDIA_ENQUEUE is True, add `media_id` to the queue.
        """
        if kwargs.get(ATTR_MEDIA_ENQUEUE):
            from pysonos.exceptions import SoCoUPnPException
            try:
                self.soco.add_uri_to_queue(media_id)
            except SoCoUPnPException:
                _LOGGER.error('Error parsing media uri "%s", '
                              "please check it's a valid media resource "
                              'supported by Sonos', media_id)
        else:
            self.soco.play_uri(media_id)

    @soco_error()
    def join(self, slaves):
        """Form a group with other players."""
        if self._coordinator:
            self.unjoin()
            group = [self]
        else:
            group = self._sonos_group.copy()

        for slave in slaves:
            if slave.unique_id != self.unique_id:
                slave.soco.join(self.soco)
                # pylint: disable=protected-access
                slave._coordinator = self
                if slave not in group:
                    group.append(slave)

        return group

    @staticmethod
    async def join_multi(hass, master, entities):
        """Form a group with other players."""
        async with hass.data[DATA_SONOS].topology_condition:
            group = await hass.async_add_executor_job(master.join, entities)
            await SonosEntity.wait_for_groups(hass, [group])

    @soco_error()
    def unjoin(self):
        """Unjoin the player from a group."""
        self.soco.unjoin()
        self._coordinator = None

    @staticmethod
    async def unjoin_multi(hass, entities):
        """Unjoin several players from their group."""
        def _unjoin_all(entities):
            """Sync helper."""
            # Unjoin slaves first to prevent inheritance of queues
            coordinators = [e for e in entities if e.is_coordinator]
            slaves = [e for e in entities if not e.is_coordinator]

            for entity in slaves + coordinators:
                entity.unjoin()

        async with hass.data[DATA_SONOS].topology_condition:
            await hass.async_add_executor_job(_unjoin_all, entities)
            await SonosEntity.wait_for_groups(hass, [[e] for e in entities])

    @soco_error()
    def snapshot(self, with_group):
        """Snapshot the state of a player."""
        from pysonos.snapshot import Snapshot

        self._soco_snapshot = Snapshot(self.soco)
        self._soco_snapshot.snapshot()
        if with_group:
            self._snapshot_group = self._sonos_group.copy()
        else:
            self._snapshot_group = None

    @staticmethod
    async def snapshot_multi(hass, entities, with_group):
        """Snapshot all the entities and optionally their groups."""
        # pylint: disable=protected-access

        def _snapshot_all(entities):
            """Sync helper."""
            for entity in entities:
                entity.snapshot(with_group)

        # Find all affected players
        entities = set(entities)
        if with_group:
            for entity in list(entities):
                entities.update(entity._sonos_group)

        async with hass.data[DATA_SONOS].topology_condition:
            await hass.async_add_executor_job(_snapshot_all, entities)

    @soco_error()
    def restore(self):
        """Restore a snapshotted state to a player."""
        from pysonos.exceptions import SoCoException

        try:
            # pylint: disable=protected-access
            self._soco_snapshot.restore()
        except (TypeError, AttributeError, SoCoException) as ex:
            # Can happen if restoring a coordinator onto a current slave
            _LOGGER.warning("Error on restore %s: %s", self.entity_id, ex)

        self._soco_snapshot = None
        self._snapshot_group = None

    @staticmethod
    async def restore_multi(hass, entities, with_group):
        """Restore snapshots for all the entities."""
        # pylint: disable=protected-access

        def _restore_groups(entities, with_group):
            """Pause all current coordinators and restore groups."""
            for entity in (e for e in entities if e.is_coordinator):
                if entity.state == STATE_PLAYING:
                    entity.media_pause()

            groups = []

            if with_group:
                # Unjoin slaves first to prevent inheritance of queues
                for entity in [e for e in entities if not e.is_coordinator]:
                    if entity._snapshot_group != entity._sonos_group:
                        entity.unjoin()

                # Bring back the original group topology
                for entity in (e for e in entities if e._snapshot_group):
                    if entity._snapshot_group[0] == entity:
                        entity.join(entity._snapshot_group)
                        groups.append(entity._snapshot_group.copy())

            return groups

        def _restore_players(entities):
            """Restore state of all players."""
            for entity in (e for e in entities if not e.is_coordinator):
                entity.restore()

            for entity in (e for e in entities if e.is_coordinator):
                entity.restore()

        # Find all affected players
        entities = set(e for e in entities if e._soco_snapshot)
        if with_group:
            for entity in [e for e in entities if e._snapshot_group]:
                entities.update(entity._snapshot_group)

        async with hass.data[DATA_SONOS].topology_condition:
            groups = await hass.async_add_executor_job(
                _restore_groups, entities, with_group)

            await SonosEntity.wait_for_groups(hass, groups)

            await hass.async_add_executor_job(_restore_players, entities)

    @staticmethod
    async def wait_for_groups(hass, groups):
        """Wait until all groups are present, or timeout."""
        # pylint: disable=protected-access

        def _test_groups(groups):
            """Return whether all groups exist now."""
            for group in groups:
                coordinator = group[0]

                # Test that coordinator is coordinating
                current_group = coordinator._sonos_group
                if coordinator != current_group[0]:
                    return False

                # Test that slaves match
                if set(group[1:]) != set(current_group[1:]):
                    return False

            return True

        try:
            with async_timeout.timeout(5):
                while not _test_groups(groups):
                    await hass.data[DATA_SONOS].topology_condition.wait()
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout waiting for target groups %s", groups)

        for entity in hass.data[DATA_SONOS].entities:
            entity.soco._zgs_cache.clear()

    @soco_error()
    @soco_coordinator
    def set_sleep_timer(self, sleep_time):
        """Set the timer on the player."""
        self.soco.set_sleep_timer(sleep_time)

    @soco_error()
    @soco_coordinator
    def clear_sleep_timer(self):
        """Clear the timer on the player."""
        self.soco.set_sleep_timer(None)

    @soco_error()
    @soco_coordinator
    def set_alarm(self, **data):
        """Set the alarm clock on the player."""
        from pysonos import alarms
        alarm = None
        for one_alarm in alarms.get_alarms(self.soco):
            # pylint: disable=protected-access
            if one_alarm._alarm_id == str(data[ATTR_ALARM_ID]):
                alarm = one_alarm
        if alarm is None:
            _LOGGER.warning("did not find alarm with id %s",
                            data[ATTR_ALARM_ID])
            return
        if ATTR_TIME in data:
            alarm.start_time = data[ATTR_TIME]
        if ATTR_VOLUME in data:
            alarm.volume = int(data[ATTR_VOLUME] * 100)
        if ATTR_ENABLED in data:
            alarm.enabled = data[ATTR_ENABLED]
        if ATTR_INCLUDE_LINKED_ZONES in data:
            alarm.include_linked_zones = data[ATTR_INCLUDE_LINKED_ZONES]
        alarm.save()

    @soco_error()
    def set_option(self, **data):
        """Modify playback options."""
        if ATTR_NIGHT_SOUND in data and self._night_sound is not None:
            self.soco.night_mode = data[ATTR_NIGHT_SOUND]

        if ATTR_SPEECH_ENHANCE in data and self._speech_enhance is not None:
            self.soco.dialog_mode = data[ATTR_SPEECH_ENHANCE]

    @property
    def device_state_attributes(self):
        """Return entity specific state attributes."""
        attributes = {
            ATTR_SONOS_GROUP: [e.entity_id for e in self._sonos_group],
        }

        if self._night_sound is not None:
            attributes[ATTR_NIGHT_SOUND] = self._night_sound

        if self._speech_enhance is not None:
            attributes[ATTR_SPEECH_ENHANCE] = self._speech_enhance

        return attributes
