# Copyright (c) 2014 Evalf
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
The log module provides print methods ``debug``, ``info``, ``user``,
``warning``, and ``error``, in increasing order of priority. Output is sent to
stdout as well as to an html formatted log file if so configured.
"""

import time, functools, itertools, io, abc, contextlib, html, urllib.parse, os, json, traceback, bdb, inspect, textwrap, builtins, hashlib
from . import core, config, warnings

LEVELS = 'error', 'warning', 'user', 'info', 'debug' # NOTE this should match the log levels defined in `nutils/_log/viewer.js`
HTMLHEAD = '''\
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, minimum-scale=1, user-scalable=no"/>
<title>{title}</title>
<script src="{viewer_js}"></script>
<link rel="stylesheet" type="text/css" href="{viewer_css}"/>
<link rel="icon" sizes="48x48" type="image/png" href="{favicon_png}"/>
</head>'''

## LOG

class Log(contextlib.ExitStack, metaclass=abc.ABCMeta):
  '''
  Base class for log objects.  A subclass should define a :meth:`context`
  method that returns a context manager which adds a contextual layer and a
  :meth:`write` method.
  '''

  def __enter__(self):
    if hasattr(self, '_old_log'):
      raise RuntimeError('This context manager is not reentrant.')
    # Replace the current log object with `self` and remember the old instance.
    global _current_log
    self._old_log = _current_log
    _current_log = self
    return super().__enter__()

  def __exit__(self, etype, value, tb):
    if not hasattr(self, '_old_log'):
      raise RuntimeError('This context manager is not yet entered.')
    if etype in (KeyboardInterrupt, SystemExit, bdb.BdbQuit):
      self.write('error', 'killed by user')
    elif etype is not None:
      self.write_post_mortem(etype, value, tb)
    # Restore the old log instance.
    global _current_log
    _current_log = self._old_log
    del self._old_log
    super().__exit__(etype, value, tb)

  @abc.abstractmethod
  def context(self, title, mayskip=False):
    '''Return a context manager that adds a contextual layer named ``title``.

    .. Note:: This function is abstract.
    '''

  @abc.abstractmethod
  def write(self, level, text):
    '''Write ``text`` with log level ``level`` to the log.

    .. Note:: This function is abstract.
    '''

  def write_post_mortem(self, etype, value, tb):
    try:
      msg = ''.join(traceback.format_exception(etype, value, tb))
    except Exception as e:
      msg = '{} (traceback failed: {})'.format(value, e)
    self.write('error', msg)

  @abc.abstractmethod
  def open(self, filename, mode, level, exists):
    '''Create file object.'''

class DataLog(Log):
  '''Output only data.'''

  def __init__(self, outdir):
    self.outdir = outdir
    super().__init__()

  def __enter__(self):
    self._open = self.enter_context(_makedirs(self.outdir, exist_ok=True))
    return super().__enter__()

  @contextlib.contextmanager
  def context(self, title, mayskip=False):
    yield

  def write(self, level, text):
    pass

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    with self._open(filename, mode, exists) as f:
      yield f

class ContextLog(Log):
  '''Base class for loggers that keep track of the current list of contexts.

  The base class implements :meth:`context` which keeps the attribute
  :attr:`_context` up-to-date.

  .. attribute:: _context

     A :class:`list` of contexts (:class:`str`\\s) that are currently active.
  '''

  def __init__(self):
    self._context = []
    super().__init__()

  def _push_context(self, title, mayskip):
    self._context.append(title)

  def _pop_context(self):
    self._context.pop()

  @contextlib.contextmanager
  def context(self, title, mayskip=False):
    '''Return a context manager that adds a contextual layer named ``title``.

    The list of currently active contexts is stored in :attr:`_context`.'''
    self._push_context(title, mayskip)
    try:
      yield
    finally:
      self._pop_context()

class ContextTreeLog(ContextLog):
  '''Base class for loggers that display contexts as a tree.

  .. automethod:: _print_push_context
  .. automethod:: _print_pop_context
  .. automethod:: _print_item
  '''

  def __init__(self):
    super().__init__()
    self._printed_context = 0

  def _pop_context(self):
    super()._pop_context()
    if self._printed_context > len(self._context):
      self._printed_context -= 1
      self._print_pop_context()

  def write(self, level, text, **kwargs):
    '''Write ``text`` with log level ``level`` to the log.

    This method makes sure the current context is printed and calls
    :meth:`_print_item`.
    '''
    from . import parallel
    if parallel.procid:
      return
    for title in self._context[self._printed_context:]:
      self._print_push_context(title)
      self._printed_context += 1
    self._print_item(level, text, **kwargs)

  @abc.abstractmethod
  def _print_push_context(self, title):
    '''Push a context to the log.

    This method is called just before the first item of this context is added
    to the log.  If no items are added to the log within this context or
    children of this context this method nor :meth:`_print_pop_context` will be
    called.

    .. Note:: This function is abstract.
    '''

  @abc.abstractmethod
  def _print_pop_context(self):
    '''Pop a context from the log.

    This method is called whenever a context is exited, but only if
    :meth:`_print_push_context` has been called before for the same context.

    .. Note:: This function is abstract.
    '''

  @abc.abstractmethod
  def _print_item(self, level, text):
    '''Add an item to the log.

    .. Note:: This function is abstract.
    '''

class StdoutLog(ContextLog):
  '''Output plain text to stream.'''

  def __init__(self, stream=None):
    self.stream = stream
    super().__init__()

  def _mkstr(self, level, text):
    return ' > '.join(self._context + ([text] if text is not None else []))

  def write(self, level, text, endl=True):
    verbose = config.verbose
    if level not in LEVELS[verbose:]:
      from . import parallel
      if parallel.procid is not None:
        text = '[{}] {}'.format(parallel.procid, text)
      s = self._mkstr(level, text)
      print(s, end='\n' if endl else '', file=self.stream)

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    yield _devnull(filename)
    self.write(level, filename)

class RichOutputLog(StdoutLog):
  '''Output rich (colored,unicode) text to stream.'''

  # color order: black, red, green, yellow, blue, purple, cyan, white

  cmap = { 'path': (2,1), 'error': (1,1), 'warning': (1,0), 'user': (3,0) }

  def __init__(self, stream=None, *, progressinterval=None):
    super().__init__(stream=stream)
    # Timestamp at which a new progress line may be written.
    self._progressupdate = 0
    # Progress update interval in seconds.
    self._progressinterval = progressinterval or getattr(config, 'progressinterval', 0.1)

  def __enter__(self):
    self.callback(print, end='\033[K', file=self.stream) # clear the progress line
    return super().__enter__()

  def _mkstr(self, level, text):
    if text is not None:
      string = ' · '.join(self._context + [text])
      n = len(string) - len(text)
      # This is not a progress line.  Reset the update timestamp.
      self._progressupdate = 0
    else:
      string = ' · '.join(self._context)
      n = len(string)
      # Don't touch `self._progressupdate` here.  Will be done in
      # `self._push_context`.
    try:
      colorid, boldid = self.cmap[level]
    except KeyError:
      return '\033[K\033[1;30m{}\033[0m{}'.format(string[:n], string[n:])
    else:
      return '\033[K\033[1;30m{}\033[{};3{}m{}\033[0m'.format(string[:n], boldid, colorid, string[n:])

  def _push_context(self, title, mayskip):
    super()._push_context(title, mayskip)
    from . import parallel
    if parallel.procid:
      return
    t = time.time()
    if not mayskip or t >= self._progressupdate:
      self._progressupdate = t + self._progressinterval
      print(self._mkstr('progress', None), end='\r', file=self.stream)

class HtmlLog(ContextTreeLog):
  '''Output html nested lists.'''

  def __init__(self, outdir, *, title='nutils', scriptname=None, funcname=None, funcargs=None):
    self._outdir = outdir
    self._title = title
    self._scriptname = scriptname
    self._funcname = funcname
    self._funcargs = funcargs
    super().__init__()

  def __enter__(self):
    self._open = self.enter_context(_makedirs(self._outdir, exist_ok=True))
    # Copy dependencies.
    paths = {}
    for filename in 'favicon.png', 'viewer.css', 'viewer.js':
      with builtins.open(os.path.join(os.path.dirname(__file__), '_log', filename), 'rb') as src:
        data = src.read()
      with self._open(hashlib.sha1(data).hexdigest() + '.' + filename.split('.')[1], 'wb', exists='skip') as dst:
        dst.write(data)
      paths[filename.replace('.', '_')] = dst.name
    # Write header.
    self._file = self.enter_context(self._open('log.html', 'w', exists='rename'))
    self._print('<!DOCTYPE html>')
    self._print('<html>')
    self._print(HTMLHEAD.format(title=html.escape(self._title), **paths))
    body_attrs = []
    if self._scriptname:
      body_attrs.append(('data-scriptname', html.escape(self._scriptname)))
      body_attrs.append(('data-latest', '../../../../log.html'))
    if self._funcname:
      body_attrs.append(('data-funcname', html.escape(self._funcname)))
    self._print(''.join(['<body'] + [' {}="{}"'.format(*item) for item in body_attrs] + ['>']))
    self._print('<div id="log">')
    if self._funcargs:
      self._print('<ul class="cmdline">')
      for name, value, annotation in self._funcargs:
        self._print(('  <li>{}={}<span class="annotation">{}</span></li>' if annotation is not inspect.Parameter.empty else '<li>{}={}</li>').format(*(html.escape(str(v)) for v in (name, value, annotation))))
      self._print('</ul>')
    self.callback(self._print, '</div></body></html>')
    return super().__enter__()

  def _print(self, *args, flush=False):
    print(*args, file=self._file)
    if flush:
      self._file.flush()

  def _print_push_context(self, title):
    self._print('<div class="context"><div class="title">{}</div><div class="children">'.format(html.escape(title)), flush=True)

  def _print_pop_context(self):
    self._print('</div><div class="end"></div></div>', flush=True)

  def _print_item(self, level, text, escape=True):
    if escape:
      text = html.escape(text)
    self._print('<div class="item" data-loglevel="{}">{}</div>'.format(LEVELS.index(level), text), flush=True)

  def write_post_mortem(self, etype, value, tb):
    'write exception nfo to html log'

    super().write_post_mortem(etype, value, tb)
    _fmt = lambda obj: '=' + ''.join(s.strip() for s in repr(obj).split('\n'))
    self._print('<div class="post-mortem">')
    self._print('EXHAUSTIVE STACK TRACE')
    self._print()
    for frame, filename, lineno, function, code_context, index in inspect.getinnerframes(tb):
      self._print('File "{}", line {}, in {}'.format(filename, lineno, function))
      self._print(html.escape(textwrap.fill(inspect.formatargvalues(*inspect.getargvalues(frame),formatvalue=_fmt), initial_indent=' ', subsequent_indent='  ', width=80)))
      if code_context:
        self._print()
        for line in code_context:
          self._print(html.escape(textwrap.fill(line.strip(), initial_indent='>>> ', subsequent_indent='    ', width=80)))
      self._print()
    self._print('</div>', flush=True)

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    with self._open(filename, mode, exists) as f:
      yield f
    self.write(level, '<a href="{href}">{name}</a>'.format(href=urllib.parse.quote(f.name), name=html.escape(filename)), escape=False)

class IndentLog(ContextTreeLog):
  '''Output indented html snippets.'''

  def __init__(self, outdir, *, progressinterval=None):
    self._outdir = outdir
    self._prefix = ''
    self._progressupdate = 0 # progress update interval in seconds
    self._progressinterval = progressinterval or getattr(config, 'progressinterval', 1)
    super().__init__()

  def __enter__(self):
    self._open = self.enter_context(_makedirs(self._outdir, exist_ok=True))
    self._logfile = self.enter_context(self._open('log.html', 'w', exists='overwrite'))
    self._progressfile = self.enter_context(self._open('progress.json', 'w', exists='overwrite'))
    return super().__enter__()

  def _print(self, *args, flush=False):
    print(*args, file=self._logfile)
    if flush:
      self._logfile.flush()

  def _print_push_context(self, title):
    title = title.replace('\n', '').replace('\r', '')
    self._print('{}c {}'.format(self._prefix, html.escape(title)), flush=True)
    self._prefix += ' '

  def _print_pop_context(self):
    self._prefix = self._prefix[:-1]

  def _print_item(self, level, text, escape=True):
    if escape:
      text = html.escape(text)
    level = html.escape(level[0])
    for line in text.splitlines():
      self._print('{}{} {}'.format(self._prefix, level, line), flush=True)
      level = '|'
    self._print_progress(level, text)
    self._progressupdate = 0

  def _push_context(self, title, mayskip):
    super()._push_context(title, mayskip)
    from . import parallel
    if parallel.procid:
      return
    t = time.time()
    if t < self._progressupdate:
      return
    self._print_progress(None, None)
    self._progressupdate = t + self._progressinterval

  def _print_progress(self, level, text):
    self._progressfile.seek(0)
    self._progressfile.truncate(0)
    json.dump(dict(logpos=self._logfile.tell(), context=self._context, text=text, level=level), self._progressfile)
    self._progressfile.write('\n')
    self._progressfile.flush()

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    with self._open(filename, mode, exists) as f:
      yield f
    self._print_item(level, '<a href="{href}">{name}</a>'.format(href=urllib.parse.quote(f.name), name=html.escape(filename)), escape=False)

class TeeLog(Log):
  '''Simultaneously interface multiple logs'''

  def __init__(self, *logs):
    self.logs = logs
    super().__init__()

  def __enter__(self):
    for log in self.logs:
      self.enter_context(log)
    return super().__enter__()

  @contextlib.contextmanager
  def context(self, title, mayskip=False):
    with contextlib.ExitStack() as stack:
      for log in self.logs:
        stack.enter_context(log.context(title, mayskip))
      yield

  def write(self, level, text):
    for log in self.logs:
      log.write(level, text)

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    with contextlib.ExitStack() as stack:
      yield _multistream(stack.enter_context(log.open(filename, mode, level, exists)) for log in self.logs)

class RecordLog(Log):
  '''
  Log object that records log messages.  All messages are forwarded to the log
  that whas active before activating this log (e.g. by ``with RecordLog() as
  record:``).  The recorded messages can be replayed to the log that's
  currently active by :meth:`replay`.

  Typical usage is caching expensive operations::

      # compute
      with RecordLog() as record:
        result = compute_something_expensive()
      raw = pickle.dumps((record, result))
      # reuse
      record, result = pickle.loads(raw)
      record.replay()

  .. Note::
     Instead of using :class:`RecordLog` and :mod:`pickle` manually, as in
     above example, we advice to use :class:`nutils.cache.FileCache` instead.

  .. Note::
     Exceptions raised while in a :meth:`Log.context` are not recorded.

  .. Note::
     Messages dispatched from forks (e.g. inside
     :meth:`nutils.parallel.pariter`) are not recorded.
  '''

  def __init__(self):
    # Replayable log messages.  Each entry is a tuple of `(cmd, *args)`, where
    # `cmd` is either 'entercontext', 'exitcontext' or 'write'.  See
    # `self.replay` below.
    self._messages = []
    # `self._contexts` is a list of entered context titles.  We keep track of
    # the titles because we delay appending the 'entercontext' command until
    # something (nonzero) is written to the log.  This is to exclude progress
    # information.  The `self._appended_contexts` index tracks the number of
    # contexts that we have appended to `self._messages`.
    self._contexts = []
    self._appended_contexts = 0
    super().__init__()

  @contextlib.contextmanager
  def context(self, title, mayskip=False):
    self._contexts.append(title)
    # We don't append 'entercontext' here.  See `self.__init__`.
    try:
      with self._old_log.context(title, mayskip):
        yield
    finally:
      self._contexts.pop()
      if self._appended_contexts > len(self._contexts):
        self._appended_contexts -= 1
        self._messages.append(('exitcontext',))

  def write(self, level, text):
    self._old_log.write(level, text)
    from . import parallel
    if not parallel.procid:
      # Append all currently entered contexts that have not been append yet
      # before appending the 'write' entry.
      for title in self._contexts[self._appended_contexts:]:
        self._messages.append(('entercontext', title))
      self._appended_contexts = len(self._contexts)
      self._messages.append(('write', level, text))

  @contextlib.contextmanager
  def open(self, filename, mode, level, exists):
    for title in self._contexts[self._appended_contexts:]:
      self._messages.append(('entercontext', title))
    self._appended_contexts = len(self._contexts)
    data = io.BytesIO() if 'b' in mode else io.StringIO()
    with self._old_log.open(filename, mode, level) as f:
      yield _multistream([f, data])
    self._messages.append(('open', filename, mode, level, exists, data.getValue()))

  def replay(self):
    '''
    Replay this recorded log in the log that's currently active.
    '''
    contexts = []
    for cmd, *args in self._messages:
      if cmd == 'entercontext':
        context = _current_log.context(*args)
        context.__enter__()
        contexts.append(context)
      elif cmd == 'exitcontext':
        contexts.pop().__exit__(None, None, None)
      elif cmd == 'write':
        _current_log.write(*args)
      elif cmd == 'open':
        filename, mode, level, exists, data = args
        with _current_log.open(filename, mode, level, exists) as f:
          f.write(data)

## INTERNAL FUNCTIONS

# Reference to the current log instance.  This is updated by the `Log`'s
# context manager, see `Log` base class.
_current_log = None

# Set a default log instance.
StdoutLog().__enter__()

def _len(iterable):
  '''Return length if available, otherwise None'''

  try:
    return len(iterable)
  except:
    return None

def _print(level, *args):
  return _current_log.write(level, ' '.join(str(arg) for arg in args))

class _multistream(io.IOBase):
  def __init__(self, streams):
    self.streams = tuple(streams)
  def __bool__(self):
    return any(self.streams)
  def write(self, data):
    for stream in self.streams:
      stream.write(data)

class _devnull(io.IOBase):
  def __init__(self, name):
    self.name = name
  def __bool__(self):
    return False
  def write(self, data):
    pass

class _makedirs:
  def __init__(self, path, exist_ok=False):
    self.path = path
    self.exist_ok = exist_ok
    super().__init__()
  def __enter__(self):
    os.makedirs(self.path, exist_ok=self.exist_ok)
    if os.open in os.supports_dir_fd and os.listdir in os.supports_fd:
      self.path = os.open(self.path, flags=os.O_RDONLY)
    return self.open
  def __exit__(self, etype, value, tb):
    if isinstance(self.path, int):
      os.close(self.path)
  def _open(self, name, *args):
    return os.open(name, *args, dir_fd=self.path) if isinstance(self.path, int) \
      else os.open(os.path.join(self.path, name), *args)
  def open(self, filename, mode, exists):
    if mode not in ('w', 'wb'):
      raise ValueError('invalid mode: {!r}'.format(mode))
    if exists not in ('overwrite', 'rename', 'skip'):
      raise ValueError('invalid exists: {!r}'.format(exists))
    if exists != 'overwrite':
      listdir = set(os.listdir(self.path))
      if filename in listdir:
        if exists == 'skip':
          return _devnull(filename)
        for filename in map('-{}'.join(os.path.splitext(filename)).format, itertools.count(1)):
          if filename not in listdir:
            break
    return builtins.open(filename, mode, opener=self._open)

## MODULE-ONLY METHODS

locals().update({ name: functools.partial(_print, name) for name in LEVELS })

def path(*args):
  warnings.deprecation("log level 'path' will be removed in the future, please use any other log level instead")
  return _print('info', *args)

def range(title, *args):
  '''Progress logger identical to built in range'''

  items = builtins.range(*args)
  for index, item in builtins.enumerate(items):
    with _current_log.context('{} {} ({:.0f}%)'.format(title, item, index*100/len(items)), mayskip=index):
      yield item

def iter(title, iterable, length=None):
  '''Progress logger identical to built in iter'''

  if length is None:
    length = _len(iterable)
  it = builtins.iter(iterable)
  for index in itertools.count():
    text = '{} {}'.format(title, index)
    if length:
      text += ' ({:.0f}%)'.format(100 * index / length)
    with _current_log.context(text, mayskip=index):
      try:
        yield next(it)
      except StopIteration:
        break

def enumerate(title, iterable):
  '''Progress logger identical to built in enumerate'''

  return iter(title, builtins.enumerate(iterable), length=_len(iterable))

def zip(title, *iterables):
  '''Progress logger identical to built in enumerate'''

  lengths = [_len(iterable) for iterable in iterables]
  return iter(title, builtins.zip(*iterables), length=all(lengths) and min(lengths))

def count(title, start=0, step=1):
  '''Progress logger identical to itertools.count'''

  for item in itertools.count(start, step):
    with _current_log.context('{} {}'.format(title, item), mayskip=item!=start):
      yield item

def title(f): # decorator
  '''Decorator, adds title argument with default value equal to the name of the
  decorated function, unless argument already exists. The title value is used
  in a static log context that is destructed with the function frame.'''

  assert getattr(f, '__self__', None) is None, 'cannot decorate bound instance method'
  default = f.__name__
  argnames = f.__code__.co_varnames[:f.__code__.co_argcount]
  if 'title' in argnames:
    index = argnames.index('title')
    if index >= len(argnames) - len(f.__defaults__ or []):
      default = f.__defaults__[index - len(argnames)]
    gettitle = lambda args, kwargs: args[index] if index < len(args) else kwargs.get('title', default)
  else:
    gettitle = lambda args, kwargs: kwargs.pop('title', default)
  @functools.wraps(f)
  def wrapped(*args, **kwargs):
    with _current_log.context(gettitle(args, kwargs)):
      return f(*args, **kwargs)
  return wrapped

def context(title, mayskip=False):
  return _current_log.context(title, mayskip)

def open(filename, mode, *, level='user', exists='rename'):
  '''Open file in logger-controlled directory.

  Args
  ----
  filename : :class:`str`
  mode : :class:`str`
      Should be either ``'w'`` (text) or ``'wb'`` (binary data).
  level : :class:`str`
      Log level in which the filename is displayed. Default: ``'user'``.
  exists : :class:`str`
      Determines how existence of ``filename`` in the output directory should
      be handled. Valid values are:

      *   ``'overwrite'``: open the file and remove current contents.

      *   ``'rename'``: change the filename by adding the smallest positive
          suffix ``n`` for which ``filename-n.ext`` does not exist.

      *   ``'skip'``: return a dummy file object, which tests as ``False`` to
          allow content creation to be skipped altogether.
  '''

  return _current_log.open(filename, mode, level, exists)

# vim:shiftwidth=2:softtabstop=2:expandtab:foldmethod=indent:foldnestmax=2
