"""
PaStA - Patch Stack Analysis

Copyright (c) OTH Regensburg, 2016-2019

Author:
  Ralf Ramsauer <ralf.ramsauer@oth-regensburg.de>

This work is licensed under the terms of the GNU GPL, version 2.  See
the COPYING file in the top-level directory.
"""

import datetime
import email
import git
import glob
import itertools
import os
import pygit2
import re

from email.charset import CHARSETS
from logging import getLogger
from subprocess import call

from pypasta.Patchwork import Patchwork
from .MailThread import MailThread
from .MessageDiff import MessageDiff, Signature
from ..Util import get_commit_hash_range, mail_parse_date

log = getLogger(__name__[-15:])

PATCH_SUBJECT_REGEX = re.compile(r'\[.*?\]:? ?(.*)')
DIFF_START_REGEX = re.compile(r'^--- \S+/.+$')
ANNOTATION_REGEX = re.compile(r'^---\s*$')


class PatchMail(MessageDiff):
    def __init__(self, mail, identifier):
        self.mail_subject = mail['Subject']
        self.patchwork_id = int(mail['X-Patchwork-Id'])

        # Get informations on the author
        date = mail_parse_date(mail['Date'], assume_epoch=True)

        author = str(mail['From'])
        author_name, author_email = email.utils.parseaddr(author)
        author = Signature(author_name, author_email, date)

        # Get the patch payload
        payload = mail.get_payload(decode=True)
        charset = mail.get_content_charset()

        if charset not in CHARSETS:
           charset = 'ascii'
        payload = payload.decode(charset, errors='ignore')

        if isinstance(payload, list):
            retval = parse_list(payload)
        elif isinstance(payload, str):
            retval = parse_single_message(payload)
        else:
            raise TypeError('Warning: unknown payload type')

        if retval is None:
            raise TypeError('Unable to split mail to msg and diff')

        msg, annotation, diff = retval

        # reconstruct commit message
        subject = self.mail_subject
        match = PATCH_SUBJECT_REGEX.match(self.mail_subject)
        if match:
            subject = match.group(1)
        msg = [subject, ''] + msg

        content = msg, annotation, diff

        super(PatchMail, self).__init__(identifier, content, author)

    def format_message(self):
        custom = ['Mail Subject: %s' % self.subject]
        return super(PatchMail, self).format_message(custom)


def parse_single_message(mail):
    # Before using splitlines(), we have to replace ASCII \f by sth. else, like
    # a whitespace. Otherwise weird things happen.
    mail = mail.replace('\f', ' ')
    mail = mail.splitlines()

    message = []
    patch = None
    annotation = None

    if mail[0].startswith('--'):
        mail.pop(0)

    for line in mail:
        if patch is None and \
                (line.startswith('diff ') or
                 DIFF_START_REGEX.match(line) or
                 line.lower().startswith('index: ')):
            patch = list()
        elif annotation is None and patch is None \
             and ANNOTATION_REGEX.match(line):
            annotation = list()
            # Skip this line, we're not interested in the --- line.
            continue

        if patch is not None:
            patch.append(line)
        elif annotation is not None:
            annotation.append(line)
        else:
            message.append(line)

    if patch:
        return message, annotation, patch

    return None


def parse_list(payload):
    if len(payload) == 1:
        retval = parse_single_message(payload[0].get_payload())
        if retval:
            return retval
        else:
            return None
    payload0 = payload[0].get_payload()
    payload1 = payload[1].get_payload()

    if not (isinstance(payload0, str) and isinstance(payload1, str)):
        return None

    payload0 = payload0.splitlines()
    payload1 = payload1.splitlines()

    if any([x.startswith('diff ') for x in payload1]):
        return payload0, payload1

    return None


def load_file(filename, must_exist=True):
    if not os.path.isfile(filename) and must_exist is False:
        return []

    with open(filename) as f:
        f = f.read().split('\n')

    while len(f) and not f[-1]:
        f.pop()

    f = [tuple(x.split(' ')) for x in f]

    return f


def load_index(basename):
    entries = load_file(basename, must_exist=False)
    ret = dict()

    for (date, message_id, location) in entries:
        dtime = datetime.datetime.strptime(date, '%Y/%m/%d')

        if message_id not in ret:
            ret[message_id] = list()

        ret[message_id].append((dtime, date, location))

    return ret


class MailContainer:
    def message_ids(self, time_window=None):
        if time_window:
            return {x[0] for x in self.index.items() if
                    any(map(
                        lambda date: time_window[0] <= date <= time_window[1],
                        [y[0] for y in x[1]]))}

        return set(self.index.keys())

    def __contains__(self, message_id):
        return message_id in self.index


class PubInbox(MailContainer):
    MESSAGE_ID_REGEX = re.compile(r'.*(<.*>).*')

    def __init__(self, listaddr, shard, d_repo, f_index):
        self.listaddr = listaddr
        self.f_index = f_index
        self.d_repo = d_repo

        self.repo = pygit2.Repository(d_repo)
        self.index = load_index(self.f_index)

        log.info('  ↪ loaded mail index for %s (shard %u): found %d mails' %
                 (listaddr, shard, len(self.index)))

    def get_blob(self, commit):
        if 'm' not in self.repo[commit].tree:
            return None

        blob = self.repo[commit].tree['m'].hex
        return self.repo[blob].data

    def get_mail_by_commit(self, commit):
        blob = self.get_blob(commit)
        if not blob:
            return None

        return email.message_from_bytes(blob)

    def get_mails_by_message_id(self, message_id):
        commits = self.get_hashes(message_id)
        return [self.get_mail_by_commit(commit) for commit in commits]

    def get_hashes(self, message_id):
        return [x[2] for x in self.index[message_id]]

    def __getitem__(self, message_id):
        commits = self.get_hashes(message_id)
        return [self.get_blob(commit) for commit in commits]

    def update(self, nofetch):
        log.info('Update list %s' % self.listname)
        repo = git.Repo(self.d_repo)
        if not nofetch:
            for remote in repo.remotes:
                remote.fetch()
        self.repo = pygit2.Repository(self.d_repo)

        known_hashes = set()
        for entry in self.index.values():
            known_hashes |= {x[2] for x in entry}

        hashes = set(get_commit_hash_range(self.d_repo, 'origin/master'))

        hashes = hashes - known_hashes
        log.info('Updating %d emails' % len(hashes))

        for hash in hashes:
            mail = self.get_mail_by_commit(hash)
            if not mail:
                log.warning('No email behind commit %s' % hash)
                continue

            if not mail['Message-ID']:
                log.warning('No Message ID in commit %s' % hash)
                continue
            message_id = mail['Message-ID']
            message_id = ''.join(message_id.split())
            match = PubInbox.MESSAGE_ID_REGEX.match(message_id)
            if match:
                message_id = match.group(1)
            else:
                log.warning('Unable to parse Message ID: %s' % message_id)
                continue

            date = mail_parse_date(mail['Date'])
            if not date:
                log.warning('Unable to parse datetime %s of %s (%s)' %
                            (mail['Date'], message_id, hash))
                continue

            format_date = date.strftime('%04Y/%m/%d')

            if message_id not in self.index:
                self.index[message_id] = list()
            self.index[message_id].append((date, format_date, hash))

        with open(self.f_index, 'w') as f:
            index = list()
            for message_id, candidates in self.index.items():
                for date, format_date, commit in candidates:
                    index.append('%s %s %s' % (format_date, message_id, commit))
            index.sort()
            f.write('\n'.join(index) + '\n')


class MboxRaw(MailContainer):
    def __init__(self, d_mbox, d_index):
        self.d_mbox = d_mbox
        self.d_mbox_raw = os.path.join(d_mbox, 'raw')
        self.d_index = d_index
        self.index = {}
        self.lists = {}
        self.raw_mboxes = []

    def f_index(self, listname):
        return os.path.join(self.d_index, 'raw.%s' % listname)

    def add_mbox(self, listname, f_mbox_raw):
        self.raw_mboxes.append((listname, f_mbox_raw))
        index = load_index(self.f_index(listname))
        log.info('  ↪ loaded mail index for %s: found %d mails' % (listname, len(index)))
        self.index = {**self.index, **index}
        return set(index.keys())

    def update(self):
        for listname, f_mbox_raw in self.raw_mboxes:
            if not os.path.exists(f_mbox_raw):
                log.error('not a file or directory: %s' % f_mbox_raw)
                quit(-1)

            log.info('Processing raw mailbox %s' % listname)
            cwd = os.getcwd()
            os.chdir(os.path.join(cwd, 'tools'))
            ret = call(['./process_mailbox_maildir.sh', listname, self.d_mbox, f_mbox_raw])
            os.chdir(cwd)
            if ret == 0:
                log.info('  ↪ done')
            else:
                log.error('Mail processor failed!')

    def __getitem__(self, message_id):
        ret = list()

        for _, date_str, md5 in self.index[message_id]:
            filename = os.path.join(self.d_mbox_raw, date_str, md5)
            with open(filename, 'rb') as f:
                ret.append(f.read())

        return ret


class Mbox:
    def __init__(self, config):
        self.threads = None
        self.patchwork = None
        self.f_mail_thread_cache = config.f_mail_thread_cache
        self.message_id_to_lists = dict()
        self.d_mbox = config.d_mbox
        self.d_invalid = os.path.join(self.d_mbox, 'invalid')
        self.d_index = os.path.join(self.d_mbox, 'index')

        log.info('Loading mailbox subsystem')

        os.makedirs(self.d_invalid, exist_ok=True)
        os.makedirs(self.d_index, exist_ok=True)

        self.invalid = set()
        for f_inval in glob.glob(os.path.join(self.d_invalid, '*')):
            self.invalid |= {x[0] for x in load_file(f_inval)}
        log.info('  ↪ loaded invalid mail index: found %d invalid mails'
                 % len(self.invalid))

        if len(config.mbox_raw):
            log.info('Loading raw mailboxes...')
        self.mbox_raw = MboxRaw(self.d_mbox, self.d_index)
        for listname, f_mbox_raw in config.mbox_raw:
            message_ids = self.mbox_raw.add_mbox(listname, f_mbox_raw)
            for message_id in message_ids:
                self.add_mail_to_list(message_id, listname)

        self.pub_in = []
        if len(config.mbox_git_public_inbox):
            log.info('Loading public inboxes')
        for host, mailinglists in config.mbox_git_public_inbox:
            for mailinglist in mailinglists:
                listaddr = '%s@%s' % (mailinglist, host)

                shard = 0
                while True:
                    d_repo = os.path.join(config.d_mbox, 'pubin', host,
                                          mailinglist, '%u.git' % shard)
                    f_index = os.path.join(config.d_mbox, 'index', 'pubin',
                                           host, mailinglist, '%u' % shard)

                    if os.path.isdir(d_repo):
                        inbox = PubInbox(listaddr, shard, d_repo, f_index)
                        for message_id in inbox.message_ids():
                            self.add_mail_to_list(message_id, mailinglist)
                        self.pub_in.append(inbox)
                    else:
                        if shard == 0:
                            log.error('Unable to find shard 0 of list %s' %
                                      listname)
                            quit()
                        break

                    shard += 1

    def load_threads(self):
        if not self.threads:
            self.threads = MailThread.load(self.f_mail_thread_cache, self)
        return self.threads

    def register_patchwork(self, config):
        self.patchwork = Patchwork(**config.patchwork)

    def add_mail_to_list(self, message_id, list):
        if message_id not in self.message_id_to_lists:
            self.message_id_to_lists[message_id] = set()
        self.message_id_to_lists[message_id].add(list)

    def __contains__(self, message_id):
        for public_inbox in self.pub_in:
            if message_id in public_inbox:
                return True

        if message_id in self.mbox_raw:
            return True

        return False

    def __getitem__(self, message_id):
        messages = self.get_messages(message_id)
        exception = None

        if len(messages) == 0:
            raise KeyError('Message not found')

        for message in messages:
            try:
                patch = PatchMail(message, message_id)
                return patch
            except Exception as e:
                exception = e

        raise exception

    def get_messages(self, message_id):
        raws = self.get_raws(message_id)

        return [email.message_from_bytes(raw) for raw in raws]

    def get_raws(self, message_id):
        raws = list()

        for public_inbox in self.pub_in:
            if message_id in public_inbox:
                raws += public_inbox[message_id]

        if message_id in self.mbox_raw:
            raws += self.mbox_raw[message_id]

        return raws

    def message_ids(self, time_window=None, allow_invalid=False):
        ids = set()

        for pub in self.pub_in:
            ids |= pub.message_ids(time_window)

        ids |= self.mbox_raw.message_ids(time_window)

        if allow_invalid:
            return ids

        return ids - self.invalid

    def update(self, nofetch):
        self.mbox_raw.update()

        for pub in self.pub_in:
            pub.update(nofetch)

    def get_lists(self, message_id):
        return self.message_id_to_lists[message_id]

    def latest_message_date(self):
        latest = None
        for mbox in itertools.chain(self.pub_in, self.mbox_raw.raw_mboxes):
            f_index = mbox.f_index if isinstance(mbox, PubInbox) \
                else self.mbox_raw.f_index(mbox[0])
            # process_mailbox_maildir.sh sorts the index lines by date in
            # ascending order
            date = load_file(f_index, must_exist=True)[-1][0]
            date = datetime.datetime.strptime(date, '%Y/%m/%d')
            if latest is None or date > latest:
                latest = date
        return latest

    def invalidate(self, invalid):
        self.invalid |= set(invalid)

        # For data persistence, note that we have to split invalid list to
        # chunks of 1.000.000 entries (~50MiB) to overcome GitHub's maximum
        # file size.
        chunksize = 1000000

        invalid = list(self.invalid)
        invalid.sort()

        invalid = [invalid[x:x+chunksize]
                   for x in range(0, len(invalid), chunksize)]

        for no, inv in enumerate(invalid):
            f_invalid = os.path.join(self.d_invalid, '%d' % no)
            with open(f_invalid, 'w') as f:
                f.write('\n'.join(inv) + '\n')
