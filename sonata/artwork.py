import logging
import os
import shutil
import threading # artwork_update starts a thread _artwork_update
from queue import PriorityQueue

from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject

from sonata import img, ui, misc, consts, mpdhelper as mpdh
from sonata import library
from sonata.pluginsystem import pluginsystem


logger = logging.getLogger(__name__)

class ArtworkThread(threading.Thread, GObject.GObject):
    # We add a custom signal here, to emit the key (a SongRecord) for lookup by
    # signal consumers of the cache
    __gsignals__ = {
        'art_ready': (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,))
    }
    def __init__(self, artwork, art_queue):
        GObject.GObject.__init__(self)
        threading.Thread.__init__(self)
        self.artwork = artwork
        self.art_queue = art_queue

    def run(self):
        while True:
            # Will block this thread until something is in the queue
            data = self.art_queue.get()[1]
            logger.info("Artwork thread getting art for {} from {}".format(
                data.artist, data.album))
            # Check if it's in the cache; due to an inability to easily replace
            # tasks in the queue to change their priority, we may have handled
            # the task with a higher priority already
            try:
                if self.artwork.get_cached_filename(data) is not None:
                    self.art_queue.task_done()
                    continue
            except KeyError:
                pass

            # Look for a local file
            loc_type, filename = self.artwork.artwork_get_local_image(
                data.path, data.artist, data.album)
            if not filename:
                # Try remote
                filename = self.artwork.target_image_filename(None, data.path,
                                                              data.artist,
                                                              data.album)
                self.artwork.artwork_download_img_to_file(data.artist,
                                                          data.album,
                                                          filename)
                loc_type, filename = self.artwork.artwork_get_local_image(
                    data.path, data.artist, data.album)

            if filename:
                self.artwork.cache[data] = filename
            else:
                self.artwork.cache[data] = None

            self.emit('art_ready', data)

            # Will notify that the item has been done
            self.art_queue.task_done()


class Artwork:

    def __init__(self, config, is_lang_rtl, schedule_gc_collect,
                 target_image_filename, imagelist_append, remotefilelist_append,
                 allow_art_search, status_is_play_or_pause,
                 get_current_song_text, album_image, tray_image,
                 fullscreen_image, fullscreen_label1, fullscreen_label2):

        self.config = config
        self.album_filename = 'sonata-album'

        # constants from main
        self.is_lang_rtl = is_lang_rtl

        # callbacks to main XXX refactor to clear this list
        self.schedule_gc_collect = schedule_gc_collect
        self.target_image_filename = target_image_filename
        self.imagelist_append = imagelist_append
        self.remotefilelist_append = remotefilelist_append
        self.allow_art_search = allow_art_search
        self.status_is_play_or_pause = status_is_play_or_pause
        self.get_current_song_text = get_current_song_text

        # local pixbufs, image file names
        self.sonatacd = Gtk.IconFactory.lookup_default('sonata-cd')
        self.sonatacd_large = Gtk.IconFactory.lookup_default('sonata-cd-large')
        self.albumpb = None
        self.currentpb = None

        # local UI widgets provided to main by getter methods
        self.albumimage = album_image
        self.albumimage.set_from_icon_set(self.sonatacd, -1)

        self.tray_album_image = tray_image

        self.fullscreenalbumimage = fullscreen_image
        self.fullscreenalbumlabel = fullscreen_label1
        self.fullscreenalbumlabel2 = fullscreen_label2
        self.fullscreen_cover_art_reset_image()
        self.fullscreen_cover_art_reset_text()

        self.info_image = None
        self.calc_info_image_size = None

        # local version of Main.songinfo mirrored by update_songinfo
        self.songinfo = None

        # local state
        self.lastalbumart = None
        self.single_img_in_dir = None
        self.misc_img_in_dir = None
        self.stop_art_update = False
        self.downloading_image = False

        self.art_queue = None
        self.art_thread = None
        self.art_queued = {}
        self.artwork_thread_init()

        # local artwork, cache for library
        self.lib_art_pb_size = consts.LIB_COVER_SIZE
        self.cache = {}

        self.artwork_load_cache()

    def set_info_image(self, info_image):
        self.info_image = info_image
        self.info_image.set_from_icon_set(self.sonatacd_large, -1)

    def set_info_imagebox(self, info_imagebox):
        self.info_imagebox = info_imagebox

    def set_calc_info_image_size(self, func):
        self.calc_info_image_size = func

    def update_songinfo(self, songinfo):
        self.songinfo = songinfo

    def on_reset_image(self, _action):
        if self.songinfo:
            if 'name' in self.songinfo:
                # Stream, remove file:
                misc.remove_file(self.artwork_stream_filename(
                    self.songinfo.name))
            else:
                # Normal song:
                misc.remove_file(self.target_image_filename())
                misc.remove_file(self.target_image_filename(
                    consts.ART_LOCATION_HOMECOVERS))
                # Use blank cover as the artwork
                dest_filename = self.target_image_filename(
                    consts.ART_LOCATION_HOMECOVERS)
                try:
                    emptyfile = open(dest_filename, 'w')
                    emptyfile.close()
                except IOError:
                    pass
            self.artwork_update(True)

    def artwork_set_tooltip_art(self, pix):
        # Set artwork
        pix = pix.new_subpixbuf(0, 0, 77, 77)
        self.tray_album_image.set_from_pixbuf(pix)
        del pix

    def artwork_stop_update(self):
        self.stop_art_update = True

    def artwork_is_downloading_image(self):
        return self.downloading_image

    def artwork_thread_init(self):
        self.art_queue = PriorityQueue()
        self.art_thread = ArtworkThread(self, self.art_queue)
        self.art_thread.daemon = True
        self.art_thread.start()

    def get_cached_filename(self, cache_key):
        try:
            filename = self.cache[cache_key]
            if not os.path.exists(filename):
                del self.cache[cache_key]
                return None
            return filename
        except:
            return None

    def get_pixbuf(self, cache_key, priority=10):
        filename = self.get_cached_filename(cache_key)
        if filename:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(filename,
                                                        self.lib_art_pb_size,
                                                        self.lib_art_pb_size)
            return self.artwork_apply_composite_case(pb,
                                                     self.lib_art_pb_size,
                                                     self.lib_art_pb_size)
        # None available, add it to the queue
        if not cache_key in self.art_queued:
            self.art_queued[cache_key] = priority
            self.art_queue.put((priority, cache_key))
        return None

    def get_cover(self, dirname, artist, album, pb_size):
        _tmp, coverfile = self.artwork_get_local_image(dirname, artist, album)
        if not coverfile:
            return (None, None)

        try:
            coverpb = GdkPixbuf.Pixbuf.new_from_file_at_size(coverfile,
                                                        pb_size, pb_size)
        except:
            # Delete bad image:
            misc.remove_file(coverfile)
            return (None, None)
        w = coverpb.get_width()
        h = coverpb.get_height()
        coverpb = self.artwork_apply_composite_case(coverpb, w, h)
        return (coverpb, coverfile)

    def library_set_cover(self, i, pb, data):
        if self.lib_model.iter_is_valid(i):
            if self.lib_model.get_value(i, 1) == data:
                self.lib_model.set_value(i, 0, pb)

    def library_get_album_cover(self, dirname, artist, album, pb_size):
        _tmp, coverfile = self.artwork_get_local_image(dirname, artist, album)
        if coverfile:
            try:
                coverpb = GdkPixbuf.Pixbuf.new_from_file_at_size(coverfile,
                                                            pb_size, pb_size)
            except:
                # Delete bad image:
                misc.remove_file(coverfile)
                return (None, None)
            w = coverpb.get_width()
            h = coverpb.get_height()
            coverpb = self.artwork_apply_composite_case(coverpb, w, h)
            return (coverpb, coverfile)
        return (None, None)

    #XXX
    def set_library_artwork_cached_filename(self, cache_key, filename):
        self.cache[cache_key] = filename

    def get_library_artwork_cached_filename(self, cache_key):
        try:
            return self.cache[cache_key]
        except:
            return None

    def get_library_artwork_cached_pb(self, cache_key, origpb):
        filename = self.get_library_artwork_cached_filename(cache_key)
        if filename is not None:
            if os.path.exists(filename):
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(filename,
                                                          self.lib_art_pb_size,
                                                          self.lib_art_pb_size)
                return self.artwork_apply_composite_case(pb,
                                                         self.lib_art_pb_size,
                                                         self.lib_art_pb_size)
            else:
                self.cache.pop(cache_key)
                return origpb
        else:
            return origpb

    def artwork_save_cache(self):
        misc.create_dir('~/.config/sonata/')
        filename = os.path.expanduser("~/.config/sonata/art_cache")
        try:
            with open(filename, 'w', encoding="utf8") as f:
                f.write(repr(self.cache))
        except IOError:
            pass

    def artwork_load_cache(self):
        filename = os.path.expanduser("~/.config/sonata/art_cache")
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding="utf8") as f:
                        self.cache = eval(f.read())
            except (IOError, SyntaxError):
                self.cache = {}
        else:
            self.cache = {}

    def artwork_update(self, force=False):
        if force:
            self.lastalbumart = None

        self.stop_art_update = False
        if not self.config.show_covers:
            return
        if not self.songinfo:
            self.artwork_set_default_icon()
            return

        if self.status_is_play_or_pause():
            thread = threading.Thread(target=self._artwork_update)
            thread.daemon = True
            thread.start()
        else:
            self.artwork_set_default_icon()

        self.fullscreen_cover_art_set_text()

    def _artwork_update(self):
        if 'name' in self.songinfo:
            # Stream
            streamfile = self.artwork_stream_filename(self.songinfo.name)
            if os.path.exists(streamfile):
                GLib.idle_add(self.artwork_set_image, streamfile, None, None,
                              None)
            else:
                self.artwork_set_default_icon()
        else:
            # Normal song:
            artist = self.songinfo.artist or ""
            album = self.songinfo.album or ""
            path = os.path.dirname(self.songinfo.file)
            if len(artist) == 0 and len(album) == 0:
                self.artwork_set_default_icon(artist, album, path)
                return
            filename = self.target_image_filename()
            if filename == self.lastalbumart:
                # No need to update..
                self.stop_art_update = False
                return
            self.lastalbumart = None
            imgfound = self.artwork_check_for_local(artist, album, path)
            if not imgfound:
                if self.config.covers_pref == consts.ART_LOCAL_REMOTE:
                    imgfound = self.artwork_check_for_remote(artist, album,
                                                             path, filename)

    def artwork_stream_filename(self, streamname):
        return os.path.join(os.path.expanduser('~/.covers'),
                "%s.jpg" % streamname.replace("/", ""))

    def artwork_check_for_local(self, artist, album, path):
        self.artwork_set_default_icon(artist, album, path)
        self.misc_img_in_dir = None
        self.single_img_in_dir = None
        location_type, filename = self.artwork_get_local_image()

        if location_type is not None and filename:
            if location_type == consts.ART_LOCATION_MISC:
                self.misc_img_in_dir = filename
            elif location_type == consts.ART_LOCATION_SINGLE:
                self.single_img_in_dir = filename
            GLib.idle_add(self.artwork_set_image, filename, artist, album, path)
            return True

        return False

    def artwork_get_local_image(self, songpath=None, artist=None, album=None):
        # Returns a tuple (location_type, filename) or (None, None).
        # Only pass a songpath, artist, and album if we don't want
        # to use info from the currently playing song.

        if songpath is None:
            songpath = os.path.dirname(self.songinfo.file)

        # Give precedence to images defined by the user's current
        # art_location config (in case they have multiple valid images
        # that can be used for cover art).
        testfile = self.target_image_filename(None, songpath, artist, album)
        if os.path.exists(testfile):
            return self.config.art_location, testfile

        # Now try all local possibilities...
        simplelocations = [consts.ART_LOCATION_HOMECOVERS,
                   consts.ART_LOCATION_COVER,
                   consts.ART_LOCATION_ALBUM,
                   consts.ART_LOCATION_FOLDER]
        for location in simplelocations:
            testfile = self.target_image_filename(location, songpath, artist,
                                                  album)
            if os.path.exists(testfile):
                return location, testfile

        testfile = self.target_image_filename(consts.ART_LOCATION_CUSTOM,
                                              songpath, artist, album)
        if self.config.art_location == consts.ART_LOCATION_CUSTOM and \
           len(self.config.art_location_custom_filename) > 0 and \
           os.path.exists(testfile):
            return consts.ART_LOCATION_CUSTOM, testfile

        if self.artwork_get_misc_img_in_path(songpath):
            return consts.ART_LOCATION_MISC, \
                    self.artwork_get_misc_img_in_path(songpath)

        path = os.path.join(self.config.musicdir[self.config.profile_num],
                            songpath)
        testfile = img.single_image_in_dir(path)
        if testfile is not None:
            return consts.ART_LOCATION_SINGLE, testfile

        return None, None

    def artwork_check_for_remote(self, artist, album, path, filename):
        self.artwork_set_default_icon(artist, album, path)
        self.artwork_download_img_to_file(artist, album, filename)
        if os.path.exists(filename):
            GLib.idle_add(self.artwork_set_image, filename, artist, album, path)
            return True
        return False

    def artwork_set_default_icon(self, artist=None, album=None, path=None):
        GLib.idle_add(self.albumimage.set_from_icon_set,
                      self.sonatacd, -1)
        GLib.idle_add(self.info_image.set_from_icon_set,
                      self.sonatacd_large, -1)
        GLib.idle_add(self.fullscreen_cover_art_reset_image)
        GLib.idle_add(self.tray_album_image.set_from_icon_set,
                      self.sonatacd, -1)

        self.lastalbumart = None

        #XXX
        # Also, update row in library:
        if artist is not None:
            cache_key = library.SongRecord(artist=artist, album=album, path=path)
            self.set_library_artwork_cached_filename(cache_key,
                                                     self.album_filename)


    def artwork_get_misc_img_in_path(self, songdir):
        path = os.path.join(self.config.musicdir[self.config.profile_num],
                            songdir)
        if os.path.exists(path):
            for name in consts.ART_LOCATIONS_MISC:
                filename = os.path.join(path, name)
                if os.path.exists(filename):
                    return filename
        return False

    def artwork_set_image(self, filename, artist, album, path,
                          info_img_only=False):
        # Note: filename arrives here is in FILESYSTEM_CHARSET, not UTF-8!
        if self.artwork_is_for_playing_song(filename):
            if os.path.exists(filename):

                # We use try here because the file might exist, but might
                # still be downloading or corrupt:
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file(filename)
                except:
                    # If we have a 0-byte file, it should mean that
                    # sonata reset the image file. Otherwise, it's a
                    # bad file and should be removed.
                    if os.stat(filename).st_size != 0:
                        misc.remove_file(filename)
                    return

                self.currentpb = pix

                if not info_img_only:
                    #XXX
                    # Store in cache
                    cache_key = library.SongRecord(artist=artist, album=album,
                                                 path=path)
                    self.set_library_artwork_cached_filename(cache_key,
                                                             filename)

                    # Artwork for tooltip, left-top of player:
                    (pix1, w, h) = img.get_pixbuf_of_size(pix, 75)
                    pix1 = self.artwork_apply_composite_case(pix1, w, h)
                    pix1 = img.pixbuf_add_border(pix1)
                    pix1 = img.pixbuf_pad(pix1, 77, 77)
                    self.albumimage.set_from_pixbuf(pix1)
                    self.artwork_set_tooltip_art(pix1)
                    del pix1

                    # Artwork for fullscreen
                    self.fullscreen_cover_art_set_image()

                # Artwork for info tab:
                if self.info_imagebox.get_size_request()[0] == -1:
                    fullwidth = self.calc_info_image_size()
                    fullwidth = max(fullwidth, 150)
                    (pix2, w, h) = img.get_pixbuf_of_size(pix, fullwidth)
                else:
                    (pix2, w, h) = img.get_pixbuf_of_size(pix, 150)
                pix2 = self.artwork_apply_composite_case(pix2, w, h)
                pix2 = img.pixbuf_add_border(pix2)
                self.info_image.set_from_pixbuf(pix2)
                del pix2
                del pix

                self.lastalbumart = filename

                self.schedule_gc_collect()

    def artwork_set_image_last(self):
        self.artwork_set_image(self.lastalbumart, None, None, None, True)

    def artwork_apply_composite_case(self, pix, w, h):
        if not pix:
            return None
        if self.config.covers_type == consts.COVERS_TYPE_STYLIZED and \
           float(w) / h > 0.5:
            # Rather than merely compositing the case on top of the artwork,
            # we will scale the artwork so that it isn't covered by the case:
            spine_ratio = float(60) / 600 # From original png
            spine_width = int(w * spine_ratio)
            case_icon = Gtk.IconFactory.lookup_default('sonata-case')

            # We use the fullscreenalbumimage because it's the biggest we have
            context = self.fullscreenalbumimage.get_style_context()
            case_pb = case_icon.render_icon_pixbuf(context, -1)
            case = case_pb.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
            # Scale pix and shift to the right on a transparent pixbuf:
            pix = pix.scale_simple(w - spine_width, h, GdkPixbuf.InterpType.BILINEAR)
            blank = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, w, h)
            blank.fill(0x00000000)
            pix.copy_area(0, 0, pix.get_width(), pix.get_height(), blank,
                          spine_width, 0)
            # Composite case and scaled pix:
            case.composite(blank, 0, 0, w, h, 0, 0, 1, 1,
                           GdkPixbuf.InterpType.BILINEAR, 250)
            del case
            del case_pb
            return blank
        return pix

    def artwork_is_for_playing_song(self, filename):
        # Since there can be multiple threads that are getting album art,
        # this will ensure that only the artwork for the currently playing
        # song is displayed
        if self.status_is_play_or_pause() and self.songinfo:
            if 'name' in self.songinfo:
                streamfile = self.artwork_stream_filename(self.songinfo.name)
                if filename == streamfile:
                    return True
            else:
                # Normal song:
                if (filename in \
                   [self.target_image_filename(consts.ART_LOCATION_HOMECOVERS),
                     self.target_image_filename(consts.ART_LOCATION_COVER),
                     self.target_image_filename(consts.ART_LOCATION_ALBUM),
                     self.target_image_filename(consts.ART_LOCATION_FOLDER),
                     self.target_image_filename(consts.ART_LOCATION_CUSTOM)] or
                    (self.misc_img_in_dir and \
                     filename == self.misc_img_in_dir) or
                    (self.single_img_in_dir and filename == \
                     self.single_img_in_dir)):
                    return True
        # If we got this far, no match:
        return False

    def artwork_download_img_to_file(self, artist, album, dest_filename,
                                     all_images=False):

        downloader = CoverDownloader(dest_filename, self.download_progress,
                                     all_images)

        self.downloading_image = True
        # Fetch covers from covers websites or such...
        cover_fetchers = pluginsystem.get('cover_fetching')
        for plugin, callback in cover_fetchers:
            logger.info("Looking for covers for %r from %r (using %s)",
                        album, artist, plugin.name)

            try:
                callback(artist, album,
                         downloader.on_save_callback, downloader.on_err_cb)
            except Exception as e:
                if logger.isEnabledFor(logging.DEBUG):
                    log = logger.exception
                else:
                    log = logger.warning

                log("Error while downloading covers from %s: %s",
                    plugin.name, e)

            if downloader.found_images:
                break

        self.downloading_image = False
        return downloader.found_images

    def download_progress(self, dest_filename_curr, i):
        # This populates Main.imagelist for the remote image window
        if os.path.exists(dest_filename_curr):
            pix = GdkPixbuf.Pixbuf.new_from_file(dest_filename_curr)
            pix = pix.scale_simple(148, 148, GdkPixbuf.InterpType.HYPER)
            pix = self.artwork_apply_composite_case(pix, 148, 148)
            pix = img.pixbuf_add_border(pix)
            if self.stop_art_update:
                del pix
                return False # don't continue to next image
            self.imagelist_append([i + 1, pix])
            del pix
            self.remotefilelist_append(dest_filename_curr)
            if i == 0:
                self.allow_art_search()

            ui.change_cursor(None)

        return True # continue to next image

    def fullscreen_cover_art_set_image(self, force_update=False):
        if self.fullscreenalbumimage.get_property('visible') or force_update:
            if self.currentpb is None:
                self.fullscreen_cover_art_reset_image()
            else:
                # Artwork for fullscreen cover mode
                (pix3, w, h) = img.get_pixbuf_of_size(self.currentpb,
                                                  consts.FULLSCREEN_COVER_SIZE)
                pix3 = self.artwork_apply_composite_case(pix3, w, h)
                pix3 = img.pixbuf_pad(pix3, consts.FULLSCREEN_COVER_SIZE,
                                      consts.FULLSCREEN_COVER_SIZE)
                self.fullscreenalbumimage.set_from_pixbuf(pix3)
                del pix3
        self.fullscreen_cover_art_set_text()

    def fullscreen_cover_art_reset_image(self):
        self.fullscreenalbumimage.set_from_icon_set(self.sonatacd_large, -1)
        self.currentpb = None

    def fullscreen_cover_art_set_text(self):
        if self.status_is_play_or_pause():
            line1, line2 = self.get_current_song_text()
            self.fullscreenalbumlabel.set_text(misc.escape_html(line1))
            self.fullscreenalbumlabel2.set_text(misc.escape_html(line2))
            self.fullscreenalbumlabel.show()
            self.fullscreenalbumlabel2.show()
        else:
            self.fullscreen_cover_art_reset_text()

    def fullscreen_cover_art_reset_text(self):
        self.fullscreenalbumlabel.hide()
        self.fullscreenalbumlabel2.hide()

    def have_last(self):
        if self.lastalbumart is not None:
            return True
        return False


class CoverDownloader:
    """Download covers and store them in temporary files"""

    def __init__(self, path, progress_cb, all_images):
        self.path = path
        self.progress_cb = progress_cb
        self.max_images = 50 if all_images else 1
        self.current = 0

    @property
    def found_images(self):
        return self.current != 0

    def on_save_callback(self, content_fp):
        """Return True to continue finding covers, False to stop finding
        covers."""

        self.current += 1
        if self.max_images > 1:
            path = self.path.replace("<imagenum>", str(self.current))
        else:
            path = self.path

        with open(path, 'wb') as fp:
            shutil.copyfileobj(content_fp, fp)

        if self.max_images > 1:
            # XXX: progress_cb makes sense only if we are downloading several
            # images, since it is supposed to update the choose artwork
            # dialog...
            return self.progress_cb(path, self.current-1)

    def on_err_cb(self, reason=None):
        """Return True to stop finding, False to continue finding covers."""
        return False
