# $Id$
"""
Copyright (C) 2010, 2011  SPARTA, Inc. dba Cobham Analytic Solutions

Permission to use, copy, modify, and distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND SPARTA DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS.  IN NO EVENT SHALL SPARTA BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""

# $Id$

from __future__ import with_statement

import os, os.path, csv, stat, sys

from django.db.models import F

import rpki, rpki.async, rpki.http, rpki.x509, rpki.left_right
from rpki.myrpki import CA, IRDB, csv_writer
from rpki.gui.app import models, settings

def conf(*handle):
    """
    Return the absolute pathname to the configuration directory for
    the given resource handle.  If additional arguments are given, they
    are taken to mean files/subdirectories.
    """
    argv = [ settings.CONFDIR ]
    argv.extend(handle)
    return os.path.join(*argv)

def read_file_from_handle(handle, fname):
    """read a filename relative to the directory for the given resource handle.  returns
    a tuple of (content, mtime)"""
    with open(conf(handle, fname), 'r') as fp:
        data = fp.read()
        mtime = os.fstat(fp.fileno())[stat.ST_MTIME]
    return data, mtime

read_identity = lambda h: read_file_from_handle(h, 'entitydb/identity.xml')[0]

def output_asns(path, handle):
    '''Write out csv file containing asns delegated to my children.'''
    qs = models.Asn.objects.filter(lo=F('hi'), allocated__in=handle.children.all())
    w = csv_writer(path)
    w.writerows([asn.allocated.handle, asn.lo] for asn in qs)
    w.close()

def output_prefixes(path, handle):
    '''Write out csv file containing prefixes delegated to my children.'''
    qs = models.AddressRange.objects.filter(allocated__in=handle.children.all())
    w = csv_writer(path)
    w.writerows([p.allocated.handle, p.as_resource_range()] for p in qs)
    w.close()

def output_roas(path, handle):
    '''Write out csv file containing my roas.'''
    qs = models.RoaRequest.objects.filter(roa__in=handle.roas.all())
    w = csv_writer(path)
    w.writerows([req.as_roa_prefix(), req.roa.asn,
                '%s-group-%d' % (handle.handle, req.roa.pk)] for req in qs)
    w.close()

def qualify_path(pfx, fname):
    """Ensure 'path' is an absolute filename."""
    return fname if fname.startswith('/') else os.path.join(pfx, fname)

def build_rpkid_caller(cfg, verbose=False):
    """
    Returns a function suitable for calling rpkid using the
    configuration information specified in the rpki.config.parser
    object.
    """
    bpki_servers_dir = cfg.get("bpki_servers_directory")
    if not bpki_servers_dir.startswith('/'):
        bpki_servers_dir = conf(cfg.get('handle'), bpki_servers_dir)

    bpki_servers = CA(cfg.filename, bpki_servers_dir)
    rpkid_base = "http://%s:%s/" % (cfg.get("rpkid_server_host"), cfg.get("rpkid_server_port"))

    return rpki.async.sync_wrapper(rpki.http.caller(
        proto       = rpki.left_right,
        client_key  = rpki.x509.RSA(PEM_file = bpki_servers.dir + "/irbe.key"),
        client_cert = rpki.x509.X509(PEM_file = bpki_servers.dir + "/irbe.cer"),
        server_ta   = rpki.x509.X509(PEM_file = bpki_servers.cer),
        server_cert = rpki.x509.X509(PEM_file = bpki_servers.dir + "/rpkid.cer"),
        url         = rpkid_base + "left-right",
        debug       = verbose))

def ghostbuster_to_vcard(gbr):
    """
    Convert a Ghostbuster object into a vCard object.
    """
    import vobject

    vcard = vobject.vCard()
    vcard.add('N').value = vobject.vcard.Name(family=gbr.family_name, given=gbr.given_name)
    # mapping from vCard type to Ghostbuster model field
    # the ORG type is a sequence of organization unit names, so
    # transform the org name into a tuple before stuffing into the
    # vCard object
    attrs = [ ('FN',    'full_name',      None),
              ('ADR',   'postal_address', None),
              ('TEL',   'telephone',      None),
              ('ORG',   'organization',   lambda x: (x,)),
              ('EMAIL', 'email_address',  None) ]
    for vtype, field, transform in attrs:
        v = getattr(gbr, field)
        if v:
            vcard.add(vtype).value = transform(v) if transform else v
    return vcard.serialize()

def configure_resources(log, handle):
    """
    This function should be called when resources for this resource
    holder have changed.  It updates IRDB and notifies rpkid to
    immediately process the changes, rather than waiting for the cron
    job to run.

    For backwards compatability (and backups), it also writes the csv
    files for use with the myrpki.py command line script.
    """

    path = conf(handle.handle)
    cfg = rpki.config.parser(os.path.join(path, 'rpki.conf'), 'myrpki')

    output_asns(qualify_path(path, cfg.get('asn_csv')), handle)
    output_prefixes(qualify_path(path, cfg.get('prefix_csv')), handle)
    output_roas(qualify_path(path, cfg.get('roa_csv')), handle)

    roa_requests = []
    for roa in handle.roas.all():
        v4 = rpki.resource_set.roa_prefix_set_ipv4()
        v6 = rpki.resource_set.roa_prefix_set_ipv6()
        for req in roa.from_roa_request.all():
            pfx = req.as_roa_prefix()
            if isinstance(pfx, rpki.resource_set.roa_prefix_ipv4):
                v4.append(pfx)
            else:
                v6.append(pfx)
        roa_requests.append((roa.asn, v4, v6))

    children = []
    for child in handle.children.all():
        asns = rpki.resource_set.resource_set_as(a.as_resource_range() for a in child.asns.all())

        v4 = rpki.resource_set.resource_set_ipv4()
        v6 = rpki.resource_set.resource_set_ipv6()
        for pfx in child.address_range.all():
            rng = pfx.as_resource_range()
            if isinstance(rng, rpki.resource_set.resource_range_ipv4):
                v4.append(rng)
            else:
                v6.append(rng)
            
        # convert from datetime.datetime to rpki.sundial.datetime
        valid_until = rpki.sundial.datetime.fromdatetime(child.valid_until)
        children.append((child.handle, asns, v4, v6, valid_until))

    ghostbusters = []
    for gbr in handle.ghostbusters.all():
        vcard = ghostbuster_to_vcard(gbr)
        parent_set = gbr.parent.all()
        if parent_set:
            for p in parent_set:
                ghostbusters.append((p, vcard))
        else:
            ghostbusters.append((None, vcard))

    irdb = IRDB(cfg)
    irdb.update(handle, roa_requests, children, ghostbusters)
    irdb.close()

    # for hosted handles, get the config for the rpkid host
    if handle.host:
        cfg = rpki.config.parser(conf(handle.host.handle, 'rpki.conf'), 'myrpki')

    # contact rpkid to request immediate update
    call_rpkid = build_rpkid_caller(cfg)
    call_rpkid(rpki.left_right.self_elt.make_pdu(action='set', self_handle=handle.handle, run_now=True))

# vim:sw=4 ts=8 expandtab