# ⁻*- coding: utf-8 -*-
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License version 3 for
# more details.
#
# You should have received a copy of the GNU General Public License version 3
# along with this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# (c) 2015 Valentin Samir
"""models for the app"""
from .default_settings import settings

from django.db import models
from django.db.models import Q
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from django.utils import timezone
from picklefield.fields import PickledObjectField

import re
import os
import sys
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from requests_futures.sessions import FuturesSession

import utils

class User(models.Model):
    """A user logged into the CAS"""
    username = models.CharField(max_length=30, unique=True)
    date = models.DateTimeField(auto_now_add=True, auto_now=True)

    @property
    def attributs(self):
        """return a fresh dict for the user attributs"""
        return utils.import_attr(settings.CAS_AUTH_CLASS)(self.username).attributs()

    def __unicode__(self):
        return self.username

    def logout(self, request):
        """Sending SLO request to all services the user logged in"""
        async_list = []
        session = FuturesSession(executor=ThreadPoolExecutor(max_workers=10))
        ticket_classes = [ServiceTicket, ProxyTicket, ProxyGrantingTicket]
        for ticket_class in ticket_classes:
            for ticket in ticket_class.objects.filter(
                    user=self,
                    validate=True if ticket_class != ProxyGrantingTicket else False,
                    single_log_out=True
            ):
                async_list.append(ticket.logout(request, session))
                ticket.delete()
        for future in async_list:
            if future:
                try:
                    future.result()
                except Exception as error:
                    error = utils.unpack_nested_exception(error)
                    messages.add_message(
                        request,
                        messages.WARNING,
                        _(u'Error during service logout %s') % error
                    )

    def get_ticket(self, ticket_class, service, service_pattern, renew):
        """
           Generate a ticket using `ticket_class` for the service
           `service` matching `service_pattern` and asking or not for
           authentication renewal with `renew
        """
        attributs = dict(
            (a.name, a.replace if a.replace else a.name) for a in service_pattern.attributs.all()
        )
        replacements = dict(
            (a.name, (a.pattern, a.replace)) for a in service_pattern.replacements.all()
        )
        service_attributs = {}
        for (key, value) in self.attributs.items():
            if key in attributs:
                if key in replacements:
                    value = re.sub(replacements[key][0], replacements[key][1], value)
                service_attributs[attributs[key]] = value
        ticket = ticket_class.objects.create(
            user=self,
            attributs=service_attributs,
            service=service,
            renew=renew,
            service_pattern=service_pattern,
            single_log_out=service_pattern.single_log_out
        )
        ticket.save()
        return ticket

    def get_service_url(self, service, service_pattern, renew):
        """Return the url to which the user must be redirected to
        after a Service Ticket has been generated"""
        ticket = self.get_ticket(ServiceTicket, service, service_pattern, renew)
        url = utils.update_url(service, {'ticket':ticket.value})
        return url

class ServicePatternException(Exception):
    pass
class BadUsername(ServicePatternException):
    """Exception raised then an non allowed username
    try to get a ticket for a service"""
    pass
class BadFilter(ServicePatternException):
    """"Exception raised then a user try
    to get a ticket for a service and do not reach a condition"""
    pass

class UserFieldNotDefined(ServicePatternException):
    """Exception raised then a user try to get a ticket for a service
    using as username an attribut not present on this user"""
    pass
class ServicePattern(models.Model):
    """Allowed services pattern agains services are tested to"""
    class Meta:
        ordering = ("pos", )

    pos = models.IntegerField(
        default=100,
        verbose_name=_(u"position")
    )
    name = models.CharField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_(u"name"),
        help_text=_(u"A name for the service")
    )
    pattern = models.CharField(
        max_length=255,
        unique=True,
        verbose_name=_(u"pattern")
    )
    user_field = models.CharField(
        max_length=255,
        default="",
        blank=True,
        verbose_name=_(u"user field"),
        help_text=_("Name of the attribut to transmit as username, empty = login")
    )
    restrict_users = models.BooleanField(
        default=False,
        verbose_name=_(u"restrict username"),
        help_text=_("Limit username allowed to connect to the list provided bellow")
    )
    proxy = models.BooleanField(
        default=False,
        verbose_name=_(u"proxy"),
        help_text=_("Proxy tickets can be delivered to the service")
    )
    proxy_callback = models.BooleanField(
        default=False,
        verbose_name=_(u"proxy callback"),
        help_text=_("can be used as a proxy callback to deliver PGT")
    )
    single_log_out = models.BooleanField(
        default=False,
        verbose_name=_(u"single log out"),
        help_text=_("Enable SLO for the service")
    )

    def __unicode__(self):
        return u"%s: %s" % (self.pos, self.pattern)

    def check_user(self, user):
        """Check if `user` if allowed to use theses services"""
        if self.restrict_users and not self.usernames.filter(value=user.username):
            raise BadUsername()
        for filtre in self.filters.all():
            if isinstance(user.attributs.get(filtre.attribut, []), list):
                attrs = user.attributs.get(filtre.attribut, [])
            else:
                attrs = [user.attributs[filtre.attribut]]
            for value in attrs:
                if re.match(filtre.pattern, str(value)):
                    break
            else:
                raise BadFilter('%s do not match %s %s' % (
                    filtre.pattern,
                    filtre.attribut,
                    user.attributs[filtre.attribut]
                ))
        if self.user_field and not user.attributs.get(self.user_field):
            raise UserFieldNotDefined()
        return True


    @classmethod
    def validate(cls, service):
        """Check if a Service Patern match `service` and
        return it, else raise `ServicePattern.DoesNotExist`"""
        for service_pattern in cls.objects.all().order_by('pos'):
            if re.match(service_pattern.pattern, service):
                return service_pattern
        raise cls.DoesNotExist()

class Username(models.Model):
    """A list of allowed usernames on a service pattern"""
    value = models.CharField(
        max_length=255,
        verbose_name=_(u"username"),
        help_text=_(u"username allowed to connect to the service")
    )
    service_pattern = models.ForeignKey(ServicePattern, related_name="usernames")

    def __unicode__(self):
        return self.value

class ReplaceAttributName(models.Model):
    """A list of replacement of attributs name for a service pattern"""
    class Meta:
        unique_together = ('name', 'replace', 'service_pattern')
    name = models.CharField(
        max_length=255,
        verbose_name=_(u"name"),
        help_text=_(u"name of an attribut to send to the service")
    )
    replace = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_(u"replace"),
        help_text=_(u"name under which the attribut will be show" \
        u"to the service. empty = default name of the attribut")
    )
    service_pattern = models.ForeignKey(ServicePattern, related_name="attributs")

    def __unicode__(self):
        if not self.replace:
            return self.name
        else:
            return u"%s → %s" % (self.name, self.replace)

class FilterAttributValue(models.Model):
    """A list of filter on attributs for a service pattern"""
    attribut = models.CharField(
        max_length=255,
        verbose_name=_(u"attribut"),
        help_text=_(u"Name of the attribut which must verify pattern")
    )
    pattern = models.CharField(
        max_length=255,
        verbose_name=_(u"pattern"),
        help_text=_(u"a regular expression")
    )
    service_pattern = models.ForeignKey(ServicePattern, related_name="filters")

    def __unicode__(self):
        return u"%s %s" % (self.attribut, self.pattern)

class ReplaceAttributValue(models.Model):
    """Replacement to apply on attributs values for a service pattern"""
    attribut = models.CharField(
        max_length=255,
        verbose_name=_(u"attribut"),
        help_text=_(u"Name of the attribut for which the value must be replace")
    )
    pattern = models.CharField(
        max_length=255,
        verbose_name=_(u"pattern"),
        help_text=_(u"An regular expression maching whats need to be replaced")
    )
    replace = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_(u"replace"),
        help_text=_(u"replace expression, groups are capture by \\1, \\2 …")
    )
    service_pattern = models.ForeignKey(ServicePattern, related_name="replacements")

    def __unicode__(self):
        return u"%s %s %s" % (self.attribut, self.pattern, self.replace)


class Ticket(models.Model):
    """Generic class for a Ticket"""
    class Meta:
        abstract = True
    user = models.ForeignKey(User, related_name="%(class)s")
    attributs = PickledObjectField()
    validate = models.BooleanField(default=False)
    service = models.TextField()
    service_pattern = models.ForeignKey(ServicePattern, related_name="%(class)s")
    creation = models.DateTimeField(auto_now_add=True)
    renew = models.BooleanField(default=False)
    single_log_out = models.BooleanField(default=False)

    def __unicode__(self):
        return u"Ticket(%s, %s)" % (self.user, self.service)

    @classmethod
    def clean(cls):
        """Remove old ticket and send SLO to timed-out services"""
        # removing old validated ticket and non validated expired tickets
        cls.objects.filter(
            (
                Q(single_log_out=False)&Q(validate=True)
            )|(
                Q(validate=False)&\
                Q(creation__lt=(timezone.now() - timedelta(seconds=settings.CAS_TICKET_VALIDITY)))
            )
        ).delete()

        # sending SLO to timed-out validated tickets
        if settings.CAS_TICKET_TIMEOUT and \
        settings.CAS_TICKET_TIMEOUT >= settings.CAS_TICKET_VALIDITY:
            async_list = []
            session = FuturesSession(executor=ThreadPoolExecutor(max_workers=10))
            queryset = cls.objects.filter(
                single_log_out=True,
                validate=True,
                creation__lt=(timezone.now() - timedelta(seconds=settings.CAS_TICKET_TIMEOUT))
            )
            for ticket in queryset:
                async_list.append(ticket.logout(None, session))
            queryset.delete()
            for future in async_list:
                if future:
                    try:
                        future.result()
                    except Exception as error:
                        sys.stderr.write("%r\n" % error)

    def logout(self, request, session):
        """Send a SLO request to the ticket service"""
        if (self.validate or isinstance(self, ProxyGrantingTicket)) and self.single_log_out:
            xml = u"""<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
     ID="%(id)s" Version="2.0" IssueInstant="%(datetime)s">
    <saml:NameID xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"></saml:NameID>
    <samlp:SessionIndex>%(ticket)s</samlp:SessionIndex>
  </samlp:LogoutRequest>""" % \
            {
                'id' : os.urandom(20).encode("hex"),
                'datetime' : timezone.now().isoformat(),
                'ticket': self.value
            }
            try:
                return session.post(
                    self.service.encode('utf-8'),
                    data={'logoutRequest':xml.encode('utf-8')},
                )
            except Exception as error:
                if request is not None:
                    error = utils.unpack_nested_exception(error)
                    messages.add_message(
                        request,
                        messages.WARNING,
                        _(u'Error during service logout %(service)s:\n%(error)s') %
                        {'service': self.service, 'error':error}
                    )
                else:
                    sys.stderr.write("%r\n" % error)

class ServiceTicket(Ticket):
    """A Service Ticket"""
    PREFIX = settings.CAS_SERVICE_TICKET_PREFIX
    value = models.CharField(max_length=255, default=utils.gen_st, unique=True)
    def __unicode__(self):
        return u"ServiceTicket(%s, %s, %s)" % (self.user, self.value, self.service)
class ProxyTicket(Ticket):
    """A Proxy Ticket"""
    PREFIX = settings.CAS_PROXY_TICKET_PREFIX
    value = models.CharField(max_length=255, default=utils.gen_pt, unique=True)
    def __unicode__(self):
        return u"ProxyTicket(%s, %s, %s)" % (self.user, self.value, self.service)
class ProxyGrantingTicket(Ticket):
    """A Proxy Granting Ticket"""
    PREFIX = settings.CAS_PROXY_GRANTING_TICKET_PREFIX
    value = models.CharField(max_length=255, default=utils.gen_pgt, unique=True)
    def __unicode__(self):
        return u"ProxyGrantingTicket(%s, %s, %s)" % (self.user, self.value, self.service)

class Proxy(models.Model):
    """A list of proxies on `ProxyTicket`"""
    class Meta:
        ordering = ("-pk", )
    url = models.CharField(max_length=255)
    proxy_ticket = models.ForeignKey(ProxyTicket, related_name="proxies")

    def __unicode__(self):
        return self.url

