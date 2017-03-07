import os
import httplib2
from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

class Remote:
  SCOPES = 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.labels https://www.googleapis.com/auth/gmail.modify'
  APPLICATION_NAME   = 'Gmailieer'
  CLIENT_SECRET_FILE = None
  authorized         = False

  special_labels = [  'INBOX',
                      'SPAM',
                      'TRASH',
                      'UNREAD',
                      'STARRED',
                      'IMPORTANT',
                      'SENT',
                      'DRAFT',
                      'CHAT',
                      'CATEGORY_PERSONAL',
                      'CATEGORY_SOCIAL',
                      'CATEGORY_PROMOTIONS',
                      'CATEGORY_UPDATES',
                      'CATEGORY_FORUMS'
                    ]

  # these cannot be changed manually
  read_only_labels = [ 'SENT', 'DRAFT' ]

  ignore_labels = set([ 'CATEGORY_PERSONAL',
                        'CATEGORY_SOCIAL',
                        'CATEGORY_PROMOTIONS',
                        'CATEGORY_UPDATES',
                        'CATEGORY_FORUMS'
                      ])
  # query to use
  query = '-in:chats'

  class BatchException (Exception):
    pass

  def __init__ (self, g):
    self.gmailieer = g
    self.CLIENT_SECRET_FILE = g.credentials_file
    self.account = g.account
    self.dry_run = g.dry_run

  def __require_auth__ (func):
    def func_wrap (self, *args, **kwargs):
      if not self.authorized:
        self.authorize ()
      return func (self, *args, **kwargs)
    return func_wrap

  @__require_auth__
  def get_labels (self):
    results = self.service.users ().labels ().list (userId = self.account).execute ()
    labels = results.get ('labels', [])

    self.labels = {}
    for l in labels:
      self.labels[l['id']] = l['name']

    return self.labels

  @__require_auth__
  def get_current_history_id (self, start):
    """
    Get the current history id of the mailbox
    """
    results = self.service.users ().history ().list (userId = self.account, startHistoryId = start).execute ()
    if 'history' in results:
      return int(results['historyId'])
    else:
      return start

  @__require_auth__
  def get_messages_since (self, start):
    """
    Get all messages changed since start historyId
    """
    results = self.service.users ().history ().list (userId = self.account, startHistoryId = start).execute ()
    if 'history' in results:
      msgs = []
      for h in results['history']:
        msgs.extend(h['messages'])
      yield msgs

    while 'nextPageToken' in results:
      pt = results['nextPageToken']
      results = self.service.users ().history ().list (userId = self.account, startHistoryId = start, pageToken = pt).execute ()

      msgs = []
      for h in results['history']:
        msgs.extend(h['messages'])
      yield msgs


  @__require_auth__
  def all_messages (self):
    """
    Get a list of all messages
    """
    results = self.service.users ().messages ().list (userId = self.account).execute ()
    if 'messages' in results:
      yield (results['resultSizeEstimate'], results['messages'])

    while 'nextPageToken' in results:
      pt = results['nextPageToken']
      results = self.service.users ().messages ().list (userId = self.account, pageToken = pt, q = self.query).execute ()

      yield (results['resultSizeEstimate'], results['messages'])

  @__require_auth__
  def get_messages (self, mids, cb, format):
    """
    Get the messages
    """

    max_req = 200
    N       = len (mids)
    i       = 0
    j       = 0

    def _cb (rid, resp, excep):
      nonlocal j
      if excep is not None:
        raise Remote.BatchException(excep)
      else:
        j += 1

      cb (resp)

    while i < N:
      n = 0
      j = i
      batch = self.service.new_batch_http_request  (callback = _cb)

      while n < max_req and i < N:
        mid = mids[i]
        batch.add (self.service.users ().messages ().get (userId = self.account,
          id = mid, format = format))
        n += 1
        i += 1

      try:
        batch.execute (http = self.http)
      except Remote.BatchException as ex:
        if max_req > 10:
          max_req = max_req / 2
          i = j # reset
          print ("reducing batch request size to: %d" % max_req)
        else:
          raise Remote.BatchException ("cannot reduce request any further")


  @__require_auth__
  def get_message (self, mid, format = 'minimal'):
    """
    Get a single message
    """
    result = self.service.users ().messages ().get (userId = self.account,
        id = mid, format = format).execute ()
    return result


  def authorize (self, reauth = False):
    if reauth:
      credential_dir = os.path.join(self.gmailieer.home, 'credentials')
      credential_path = os.path.join(credential_dir,
          'gmailieer.json')
      if os.path.exists (credential_path):
        print ("reauthorizing..")
        os.unlink (credential_path)

    self.credentials = self.__get_credentials__ ()
    self.http = self.credentials.authorize (httplib2.Http())
    self.service = discovery.build ('gmail', 'v1', http = self.http)
    self.authorized = True

  def __get_credentials__ (self):
    """
    Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    credential_dir = os.path.join(self.gmailieer.home, 'credentials')
    if not os.path.exists(credential_dir):
      os.makedirs(credential_dir)

    credential_path = os.path.join(credential_dir,
        'gmailieer.json')

    store = Storage(credential_path)
    credentials = store.get()
    if not credentials or credentials.invalid:
      flow = client.flow_from_clientsecrets(self.CLIENT_SECRET_FILE, self.SCOPES)
      flow.user_agent = self.APPLICATION_NAME
      credentials = tools.run_flow(flow, store, flags = self.gmailieer.args)
      print('Storing credentials to ' + credential_path)
    return credentials

  @__require_auth__
  def update (self, m):
    """
    Gets a message and checks which labels it should add and which to delete.
    """

    # get gmail id
    fname = m.get_filename ()
    mid   = os.path.basename (fname).split (':')[0]

    # first get message and figure out what labels it has now
    r = self.get_message (mid)
    labels = r['labelIds']
    labels = [self.labels[l] for l in labels]

    # remove ignored labels
    labels = set (labels)
    labels = labels - self.ignore_labels

    # translate to notmuch tags
    labels = [self.gmailieer.local.translate_labels.get (l, l) for l in labels]

    # this is my weirdness
    if self.gmailieer.local.state.replace_slash_with_dot:
      labels = [l.replace ('/', '.') for l in labels]

    labels = set(labels)

    # current tags
    tags = set(m.get_tags ())

    # remove special notmuch tags
    tags = tags - self.gmailieer.local.ignore_labels

    add = list(tags - labels)
    rem = list(labels - tags)

    # translate back to gmail labels
    add = [self.gmailieer.local.labels_translate.get (k, k) for k in add]
    rem = [self.gmailieer.local.labels_translate.get (k, k) for k in rem]

    if self.gmailieer.local.state.replace_slash_with_dot:
      add = [a.replace ('.', '/') for a in add]
      rem = [r.replace ('.', '/') for r in rem]

    if len(add) > 0 or len(rem) > 0:
      if self.dry_run:
        print ("(dry-run) mid: %s: add: %s, remove: %s" % (mid, str(add), str(rem)))
      else:
        self.__push_tags__ (mid, add, rem)

      return True
    else:
      return False

  @__require_auth__
  def __push_tags__ (self, mid, add, rem):
    """
    Push message changes (these are currently not batched)"
    """

    add = [self.labels[a] for a in add]
    rem = [self.labels[r] for r in rem]

    body = { 'addLabelIds' : add,
             'removeLabelIds' : rem }

    result = self.service.users ().messages ().modify (userId = self.account,
        id = mid, body = body).execute ()

    return result

