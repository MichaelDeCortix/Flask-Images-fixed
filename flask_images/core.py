from __future__ import division

from cStringIO import StringIO
from subprocess import call
from urllib import urlencode, quote as urlquote
from urllib2 import urlopen
from urlparse import urlparse
import base64
import datetime
import errno
import hashlib
import logging
import math
import os
import re
import struct
import sys

from PIL import Image
from flask import request, current_app, send_file, abort
from itsdangerous import Signer, constant_time_compare

from . import modes
from .size import Size


log = logging.getLogger(__name__)


def encode_int(value):
    return base64.urlsafe_b64encode(struct.pack('>I', int(value))).rstrip('=').lstrip('A')


def makedirs(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


# We must whitelist schemes which are permitted, otherwise craziness (such as
# allowing access to the filesystem) may ensue.
ALLOWED_SCHEMES = set(('http', 'https', 'ftp'))

# The options which we immediately recognize and shorten.
LONG_TO_SHORT = dict(
    background='b',
    cache='c',
    enlarge='e',
    format='f',
    height='h',
    mode='m',
    quality='q',
    transform='x',
    url='u',
    version='v',
    width='w',
    # signature -> 's', but should not be here.
)
SHORT_TO_LONG = dict((v, k) for k, v in LONG_TO_SHORT.iteritems())

TRANSFORM_AXIS = {
    Image.EXTENT: (0, 1, 0, 1),
    Image.AFFINE: (None, None, 0, None, None, 1),
    Image.QUAD: (0, 1, 0, 1, 0, 1, 0, 1),
    Image.PERSPECTIVE: (None, None, None, None, None, None, None, None),
    # Image.MESH: ???
}


class Images(object):
    
    
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['images'] = self

        app.config.setdefault('IMAGES_URL', '/imgsizer') # This is historical.
        app.config.setdefault('IMAGES_NAME', 'images')
        app.config.setdefault('IMAGES_PATH', ['static'])
        app.config.setdefault('IMAGES_CACHE', '/tmp/flask-images')
        app.config.setdefault('IMAGES_MAX_AGE', 3600)

        app.add_url_rule(app.config['IMAGES_URL'] + '/<path:path>', app.config['IMAGES_NAME'], self.handle_request)
        app.url_build_error_handlers.append(self.build_error_handler)

        if hasattr(app, 'add_template_global'): # Flask >= 0.10
            app.add_template_global(resized_img_src)
            app.add_template_global(resized_img_size)
            app.add_template_global(resized_img_attrs)
        else:
            ctx = {
                'resized_img_src': resized_img_src,
                'resized_img_size': resized_img_size,
                'resized_img_attrs': resized_img_attrs,
            }
            app.context_processor(lambda: ctx)


    def build_error_handler(self, error, endpoint, values):

        # See if we were asked for "images" or "images.<mode>".
        m = re.match(r'^%s(?:\.(%s))?$' % (
            re.escape(current_app.config['IMAGES_NAME']),
            '|'.join(re.escape(mode) for mode in modes.ALL)
        ), endpoint)
        if m:
            
            filename = values.pop('filename')

            # This is slightly awkward, but I want to trigger the built-in
            # TypeError if you use the "images.<mode>" method AND provide
            # a "mode" kwarg.
            mode = m.group(1)
            if mode:
                return self.build_url(filename, mode=mode, **values)
            else:
                return self.build_url(filename, **values)


        return None

    def build_url(self, local_path, **kwargs):

        # Make the path relative.
        local_path = local_path.strip('/')

        # We complain when we see non-normalized paths, as it is a good
        # indicator that unsanitized data may be getting through.
        # Mutating the scheme syntax to match is a little gross, but it works
        # for today.
        norm_path = os.path.normpath(local_path)
        if local_path.replace('://', ':/') != norm_path or norm_path.startswith('../'):
            raise ValueError('path is not normalized')

        external = kwargs.pop('external', None) or kwargs.pop('_external', None)
        scheme = kwargs.pop('scheme', None)
        if scheme and not external:
            raise ValueError('cannot specify scheme without external=True')
        if kwargs.get('_anchor'):
            raise ValueError('images have no _anchor')
        if kwargs.get('_method'):
            raise ValueError('images have no _method')
        
        # Remote URLs are encoded into the query.
        parsed = urlparse(local_path)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme not in ALLOWED_SCHEMES:
                raise ValueError('scheme %r is not allowed' % parsed.scheme)
            kwargs['url'] = local_path
            local_path = '_' # Must be something.

        # Local ones are not.
        else:
            abs_path = self.find_img(local_path)
            if abs_path:
                kwargs['version'] = encode_int(int(os.path.getmtime(abs_path)))
        
        # Prep the cache flag, which defaults to True.
        cache = kwargs.pop('cache', True)
        if not cache:
            kwargs['cache'] = ''

        # Prep the enlarge flag, which defaults to False.
        enlarge = kwargs.pop('enlarge', False)
        if enlarge:
            kwargs['enlarge'] = '1'

        # Prep the transform, which is a set of delimited strings.
        transform = kwargs.get('transform')
        if transform:
            if isinstance(transform, basestring):
                transform = re.split(r'[,;:_ ]', transform)
            # This is a strange character, but we won't be using it and it
            # doesn't escape.
            kwargs['transform'] = '_'.join(map(str, transform))

        # Sign the query.
        public_kwargs = (
            (LONG_TO_SHORT.get(k, k), v)
            for k, v in kwargs.iteritems()
            if v is not None and not k.startswith('_')
        )
        query = urlencode(sorted(public_kwargs), True)
        signer = Signer(current_app.secret_key)
        sig = signer.get_signature('%s?%s' % (local_path, query))

        url = '%s/%s?%s&s=%s' % (
            current_app.config['IMAGES_URL'],
            urlquote(local_path),
            query,
            sig,
        )

        if external:
            url = '%s://%s%s/%s' % (
                scheme or request.scheme,
                request.host,
                request.script_root,
                url.lstrip('/')
            )

        return url
        
    def find_img(self, local_path):
        local_path = os.path.normpath(local_path.lstrip('/'))
        for path_base in current_app.config['IMAGES_PATH']:
            path = os.path.join(current_app.root_path, path_base, local_path)
            if os.path.exists(path):
                return path
    
    def calculate_size(self, path, **kw):
        return Size(path=self.find_img(path), **kw)

    def resize(self, image, background=None, **kw):
        
        size = Size(image=image, **kw)

        # Get into the right colour space.
        if not image.mode.upper().startswith('RGB'):
            image = image.convert('RGBA')

        # Apply any requested transform.
        if size.transform:
            transform = self.transform
            flag = getattr(Image, transform[0].upper())
            try:
                axis = (None, 0, 1) + TRANSFORM_AXIS[flag]
            except KeyError:
                raise ValueError('unknown transform %r' % transform[0])
            if len(transform) != len(axis):
                raise ValueError('expected %d values. got %d' % (len(axis), len(transform)))
            for i in xrange(1, len(transform)):
                v = transform[i]
                if isinstance(v, basestring):
                    if v.endswith('%'):
                        if axis[i] is None:
                            raise ValueError('unknown dimension for %s value %d' % (transform[0], i))
                        transform[i] = image.size[axis[i]] * float(v[:-1]) / 100
                    else:
                        transform[i] = float(v)
            image = image.transform(
                (int(transform[1] or image.size[0]), int(transform[2] or image.size[1])),
                flag,
                transform[3:],
                Image.BILINEAR,
            )
        
        # Handle the easy cases.
        if size.mode in (modes.RESHAPE, None) or size.req_width is None or size.req_height is None:
            return image.resize((size.width, size.height), Image.ANTIALIAS)

        if size.mode not in (modes.FIT, modes.PAD, modes.CROP):
            raise ValueError('unknown mode %r' % size.mode)

        if image.size != (size.op_width, size.op_height):
            image = image.resize((size.op_width, size.op_height), Image.ANTIALIAS)
        
        if size.mode == modes.FIT:
            return image

        elif size.mode == modes.PAD:
            pad_color = str(background or 'black')
            padded = Image.new('RGBA', (size.width, size.height), pad_color)
            padded.paste(image, (
                (size.width  - size.op_width ) // 2,
                (size.height - size.op_height) // 2
            ))
            return padded
            
        elif size.mode == modes.CROP:

            dx = (size.op_width  - size.width ) // 2
            dy = (size.op_height - size.height) // 2
            return image.crop(
                (dx, dy, dx + size.width, dy + size.height)
            )
            
        else:
            raise RuntimeError('unhandled mode %r' % size.mode)
    

    def handle_request(self, path):

        # Verify the signature.
        query = dict(request.args.iteritems())
        old_sig = str(query.pop('s', None))
        if not old_sig:
            abort(404)
        signer = Signer(current_app.secret_key)
        new_sig = signer.get_signature('%s?%s' % (path, urlencode(sorted(query.iteritems()), True)))
        if not constant_time_compare(old_sig, new_sig):
            abort(404)
        
        # Expand kwargs.
        query = dict((SHORT_TO_LONG.get(k, k), v) for k, v in query.iteritems())

        remote_url = query.get('url')
        if remote_url:

            # This is redundant for newly built URLs, but not for those which
            # have already been generated and cached.
            parsed = urlparse(remote_url)
            if parsed.scheme not in ALLOWED_SCHEMES:
                abort(404)

            # Download the remote file.
            makedirs(current_app.config['IMAGES_CACHE'])
            path = os.path.join(
                current_app.config['IMAGES_CACHE'],
                hashlib.md5(remote_url).hexdigest() + os.path.splitext(parsed.path)[1]
            )

            if not os.path.exists(path):
                log.info('downloading %s' % remote_url)
                tmp_path = path + '.tmp-' + str(os.getpid())
                fh = open(tmp_path, 'wb')
                fh.write(urlopen(remote_url).read())
                fh.close()
                call(['mv', tmp_path, path])
        else:
            path = self.find_img(path)
            if not path:
                abort(404) # Not found.

        raw_mtime = os.path.getmtime(path)
        mtime = datetime.datetime.utcfromtimestamp(raw_mtime)
        # log.debug('last_modified: %r' % mtime)
        # log.debug('if_modified_since: %r' % request.if_modified_since)
        if request.if_modified_since and request.if_modified_since >= mtime:
            return '', 304
        
        mode = query.get('mode')

        transform = query.get('transform')
        transform = re.split(r'[;,_/ ]', transform) if transform else None

        background = query.get('background')
        width = query.get('width')
        width = int(width) if width else None
        height = query.get('height')
        height = int(height) if height else None
        quality = query.get('quality')
        quality = int(quality) if quality else 75
        format = (query.get('format', '') or os.path.splitext(path)[1][1:] or 'jpeg').lower()
        format = {'jpg' : 'jpeg'}.get(format, format)
        has_version = 'version' in query
        use_cache = query.get('cache', True)
        enlarge = query.get('enlarge', False)

        if use_cache:
            cache_key_parts = [path, mode, width, height, quality, format, background]
            if transform:
                cache_key_parts.append(transform)
            cache_key = hashlib.md5(repr(tuple(cache_key_parts))).hexdigest()
            cache_dir = os.path.join(current_app.config['IMAGES_CACHE'], cache_key[:2])
            cache_path = os.path.join(cache_dir, cache_key + '.' + format)
            cache_mtime = os.path.getmtime(cache_path) if os.path.exists(cache_path) else None
        
        mimetype = 'image/%s' % format
        cache_timeout = 31536000 if has_version else current_app.config['IMAGES_MAX_AGE']

        if not use_cache or not cache_mtime or cache_mtime < raw_mtime:
            
            log.info('resizing %r for %s' % (path, query))
            image = Image.open(path)
            image = self.resize(image,
                background=background,
                enlarge=enlarge,
                height=height,
                mode=mode,
                transform=transform,
                width=width,
            )

            if not use_cache:
                fh = StringIO()
                image.save(fh, format, quality=quality)
                return fh.getvalue(), 200, [
                    ('Content-Type', mimetype),
                    ('Cache-Control', cache_timeout),
                ]
            
            makedirs(cache_dir)
            cache_file = open(cache_path, 'wb')
            image.save(cache_file, format, quality=quality)
            cache_file.close()
        
        return send_file(cache_path, mimetype=mimetype, cache_timeout=cache_timeout)



def resized_img_size(path, **kw):
    self = current_app.extensions['images']
    return self.calculate_size(path, **kw)

def resized_img_attrs(path, retina=None, width=None, height=None, enlarge=False, retina_quality=None, **kw):
    
    self = current_app.extensions['images']

    page = image = self.calculate_size(
        path,
        width=width,
        height=height,
        enlarge=enlarge,
        _shortcut=True,
        **kw
    )

    if retina:

        retina_size = self.calculate_size(
            path,
            width=retina * width if width else None,
            height=retina * height if height else None,
            enlarge=enlarge,
            _shortcut=True,
            **kw
        )

        # If the larger size works.
        if enlarge or not retina_size.needs_enlarge:
            image = retina_size
            if retina_quality:
                kw['quality'] = retina_quality
        else:
            retina = False

    return {

        'dataRetinaScale': retina,
        'dataOriginalWidth': image.image_width,
        'dataOriginalHeight': image.image_height,

        'width': page.width,
        'height': page.height,
        'src': self.build_url(
            path,
            width=int(image.req_width),
            height=int(image.req_height),
            enlarge=enlarge,
            **kw
        ),
    
    }

def resized_img_src(path, **kw):
    self = current_app.extensions['images']
    return self.build_url(path, **kw)


