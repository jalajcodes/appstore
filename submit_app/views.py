from zipfile import ZipFile 
from os.path import basename
from urllib import urlopen

from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponseBadRequest, HttpResponseForbidden
from django.conf import settings
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404

from util.view_util import html_response, json_response, get_object_or_none
from util.id_util import fullname_to_name
from apps.models import Release, App
from models import AppPending
from .pomparse import PomAttrNames, parse_pom
from .processjar import process_jar

# Presents an app submission form and accepts app submissions.
@login_required
def submit_app(request):
    context = dict()
    if request.method == 'POST':
        expect_app_name = request.POST.get('expect_app_name')
        f = request.FILES.get('file')
        if f:
            try:
                fullname, version, works_with, app_dependencies, has_export_pkg = process_jar(f, expect_app_name)
                pending = _create_pending(request.user, fullname, version, works_with, app_dependencies, f)
                _send_email_for_pending(pending)
                if has_export_pkg:
                    return HttpResponseRedirect(reverse('submit-api', args=[pending.id]))
                else:
                    return HttpResponseRedirect(reverse('confirm-submission', args=[pending.id]))
            except ValueError, e:
                context['error_msg'] = str(e)
    else:
        expect_app_name = request.GET.get('expect_app_name')
        if expect_app_name:
            context['expect_app_name'] = expect_app_name
    return html_response('upload_form.html', context, request)

def _user_cancelled(request, pending):
    pending.delete_files()
    pending.delete()
    return HttpResponseRedirect(reverse('submit-app'))

def _user_accepted(request, pending):
    app = get_object_or_none(App, name = fullname_to_name(pending.fullname))
    if app:
        if not app.is_editor(request.user):
            return HttpResponseForbidden('You are not authorized to add releases, because you are not an editor')
        pending.make_release(app)
        pending.delete_files()
        pending.delete()
        return HttpResponseRedirect(reverse('app_page_edit', args=[app.name]) + '?upload_release=true')
    else:
        return html_response('submit_done.html', {'app_name': pending.fullname}, request)

def confirm_submission(request, id):
    pending = get_object_or_404(AppPending, id = int(id))
    if not pending.can_confirm(request.user):
        return HttpResponseForbidden('You are not authorized to view this page')
    action = request.POST.get('action')
    if action:
        if action == 'cancel':
            return _user_cancelled(request, pending)
        elif action == 'accept':
            return _user_accepted(request, pending)
    pom_attrs = None
    if pending.pom_xml_file:
        pending.pom_xml_file.open(mode = 'r')
        pom_attrs = parse_pom(pending.pom_xml_file)
        pending.pom_xml_file.close()
    return html_response('confirm.html', {'pending': pending, 'pom_attrs': pom_attrs}, request)

def _create_pending(submitter, fullname, version, cy_works_with, app_dependencies, release_file):
    name = fullname_to_name(fullname)
    app = get_object_or_none(App, name = name)
    if app:
        if not app.is_editor(submitter):
            raise ValueError('cannot be accepted because you are not an editor')
        release = get_object_or_none(Release, app = app, version = version)
        if release and release.active:
            raise ValueError('cannot be accepted because the app %s already has a release with version %s. You can delete this version by going to the Release History tab in the app edit page' % (app.fullname, version))

    pending = AppPending.objects.create(submitter      = submitter,
                                        fullname       = fullname,
                                        version        = version,
                                        cy_works_with  = cy_works_with)
    for dependency in app_dependencies:
        pending.dependencies.add(dependency)
    pending.release_file.save(basename(release_file.name), release_file)
    pending.save()
    return pending

def _send_email_for_pending(pending):
    msg = u"""
The following app has been submitted: 
    ID: {id}
    Name: {fullname}
    Version: {version}
    Submitter: {submitter_name} {submitter_email}
""".format(id = pending.id, fullname = pending.fullname, version = pending.version, submitter_name = pending.submitter.username, submitter_email = pending.submitter.email)
    send_mail('Cytoscape App Store - App Submitted', msg, settings.EMAIL_ADDR, [settings.CONTACT_EMAIL], fail_silently=False)

def _verify_javadocs_jar(file):
    error_msg = None
    file.open(mode = 'rb')
    try:
        zip = ZipFile(file, 'r')
        for name in zip.namelist():
            pathpieces = name.split('/')
            if name.startswith('/') or '..' in pathpieces:
                error_msg = 'The zip archive has a file path that is illegal: %s' % name
                break
        zip.close()
    except:
        error_msg = 'The Javadocs Jar file you submitted is not a valid jar/zip file'
    file.close()
    return error_msg

def submit_api(request, id):
    pending = get_object_or_404(AppPending, id = int(id))
    if not pending.can_confirm(request.user):
        return HttpResponseForbidden('You are not authorized to view this page')

    error_msg = None
    if request.POST.get('dont_submit') != None:
        return HttpResponseRedirect(reverse('confirm-submission', args=[pending.id]))
    elif request.POST.get('submit') != None:
        pom_xml_f = request.FILES.get('pom_xml')
        javadocs_jar_f = request.FILES.get('javadocs_jar')
        if pom_xml_f and javadocs_jar_f:
            if not error_msg:
                pom_xml_f.open(mode = 'r')
                pom_attrs = parse_pom(pom_xml_f)
                if len(pom_attrs) != len(PomAttrNames):
                    error_msg = 'pom.xml is not valid; it must have these tags under &lt;project&gt;: ' + ', '.join(PomAttrNames)
                pom_xml_f.close()

            if not error_msg:
                error_msg = _verify_javadocs_jar(javadocs_jar_f)

            if not error_msg:
                pending.pom_xml_file.save(basename(pom_xml_f.name), pom_xml_f)
                pending.javadocs_jar_file.save(basename(javadocs_jar_f.name), javadocs_jar_f)
                return HttpResponseRedirect(reverse('confirm-submission', args=[pending.id]))

    return html_response('submit_api.html', {'pending': pending, 'error_msg': error_msg}, request)

def _send_email_for_accepted_app(to_email, from_email, app_fullname, app_name, server_url):
    subject = u'Cytoscape App Store - {app_fullname} Has Been Approved'.format(app_fullname = app_fullname)
    app_url = reverse('app_page', args=[app_name])
    msg = u"""Your app has been approved! Here is your app page:

  {server_url}{app_url}
            
To edit your app page:
 1. Go to {server_url}{app_url}
 2. Sign in as {author_email}
 3. Under the "Editor's Actions" yellow button on the top-right, choose "Edit this page".

Make sure to add some tags to your app and a short app description, which is located
right below the app name. You can also add screenshots, details about your app,
and an icon to make your app distinguishable.

If you would like other people to be able to edit the app page, have them sign in
to the App Store, then add their email addresses to the Editors box, located in
the top-right.

- Cytoscape App Store Team
""".format(app_url = app_url, author_email = to_email, server_url = server_url)
    send_mail(subject, msg, from_email, [to_email])

def _get_server_url(request):
    name = request.META['SERVER_NAME']
    port = request.META['SERVER_PORT']
    if port == '80':
        return 'http://%s' % name
    else:
        return 'http://%s:%s' % (name, port)

def _pending_app_accept(pending, request):
    name = fullname_to_name(pending.fullname)
    # we always create a new app, because only new apps require accepting
    app = App.objects.create(fullname = pending.fullname, name = name)
    app.editors.add(pending.submitter)
    app.save()

    pending.make_release(app)
    pending.delete_files()
    pending.delete()

    server_url = _get_server_url(request)
    _send_email_for_accepted_app(pending.submitter.email, settings.CONTACT_EMAIL, app.fullname, app.name, server_url)

def _pending_app_decline(pending_app, request):
    pending_app.delete_files()
    pending_app.delete()

_PendingAppsActions = {
    'accept': _pending_app_accept,
    'decline': _pending_app_decline,
}

@login_required
def pending_apps(request):
    if not request.user.is_staff:
        return HttpResponseForbidden()
    if request.method == 'POST':
        action = request.POST.get('action')
        if not action:
            return HttpResponseBadRequest('action must be specified')
        if not action in _PendingAppsActions:
            return HttpResponseBadRequest('invalid action--must be: %s' % ', '.join(_PendingAppsActions.keys()))
        pending_id = request.POST.get('pending_id')
        if not pending_id:
            return HttpResponseBadRequest('pending_id must be specified')
        try:
            pending_app = AppPending.objects.get(id = int(pending_id))
        except AppPending.DoesNotExist, ValueError:
            return HttpResponseBadRequest('invalid pending_id')
        _PendingAppsActions[action](pending_app, request)
        if request.is_ajax():
            return json_response(True)
            
    pending_apps = AppPending.objects.all()
    return html_response('pending_apps.html', {'pending_apps': pending_apps}, request)

AppRepoUrl = 'http://code.cytoscape.org/nexus/content/repositories/apps'

def _get_deploy_url(groupId, artifactId, version):
    return '/'.join((AppRepoUrl, groupId.replace('.', '/'), artifactId, version))

def _url_exists(url):
    try:
        reader = urlopen(url)
        if reader.getcode() == 200:
            return True
    except:
        pass
    return False

def artifact_exists(request):
    if request.method != 'POST':
        return HttpResponseBadRequest('no data')
    postLookup = request.POST.get
    groupId, artifactId, version = postLookup('groupId'), postLookup('artifactId'), postLookup('version') 
    if not groupId or not artifactId or not version:
        return HttpResponseBadRequest('groupId, artifactId, or version not specified')
    deployUrl = _get_deploy_url(groupId, artifactId, version)
    return json_response(_url_exists(deployUrl))
