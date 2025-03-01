import os
from binascii import hexlify
import tempfile
from django.shortcuts import render, redirect
from django.views import View
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.urls import reverse
from django.conf import settings
from django.http import Http404
from django.utils.safestring import mark_safe

from .view_helpers import *
from .models import *
from .forms import *
from lib import redis
from lib.octoprint_comm import *
from .telegram_bot import bot_name
from lib.file_storage import save_file_obj
from app.tasks import preprocess_timelapse

# Create your views here.
def index(request):
    if request.user.is_authenticated and not request.user.consented_at:
        return redirect('/consent/')
    else:
        return redirect('/printers/')

@login_required
def priner_auth_token(request, pk):
    pk_filter = {}
    if pk != 0:
        pk_filter = dict(pk=pk)
    printers = Printer.objects.filter(user=request.user, **pk_filter)

    if printers.count() == 0:
        messages.error(request, 'You need to add a printer to get its secret token.')
        return redirect(reverse('printers'))

    return render(request, 'printer_auth_token.html', {'printers': printers})

@login_required
def printers(request):
    if not request.user.consented_at:
        return redirect('/consent/')

    printers = request.user.printer_set.order_by('-created_at').all()
    for printer in printers:
        p_settings = redis.printer_settings_get(printer.id)
        printer.settings = dict((key, p_settings.get(key, 'False') == 'True') for key in ('webcam_flipV', 'webcam_flipH', 'webcam_rotate90'))
        printer.settings.update(dict(ratio169=p_settings.get('webcam_streamRatio', '4:3') == '16:9'))

    if Printer.with_archived.filter(user=request.user, archived_at__isnull=False).count() > 0:
        messages.warning(request, mark_safe('Some of your printers have been archived. <a href="/ent/printers/archived/">Find them here.</a>'))

    return render(request, 'printer_list.html', {'printers': printers})

@login_required
def edit_printer(request, pk):
    if pk == 'new':
        printer = None
        template = 'printer_wizard.html'
    else:
        printer = get_printer_or_404(int(pk), request)
        template = 'printer_wizard.html' if request.GET.get('wizard', False) else 'printer_edit.html'

    form = PrinterForm(request.POST or None, request.FILES or None, instance=printer)
    if request.method == "POST":
        if form.is_valid():
            if pk == 'new':
                printer = form.save(commit=False)
                printer.user = request.user
                printer.auth_token = hexlify(os.urandom(10)).decode()
                form.save()
                return redirect('/printers/{}/?wizard=True#step-2'.format(printer.id))
            else:
                form.save()
                if not request.GET.get('wizard', False):
                    messages.success(request, 'Printer settings have been updated successfully!')

    return render(request, template, {'form': form})

@login_required
def delete_printer(request, pk=None):
    get_printer_or_404(pk, request).delete()
    return redirect('/printers/')

@login_required
def cancel_printer(request, pk):
    printer = get_printer_or_404(pk, request)
    succeeded, user_credited = printer.cancel_print()
    if succeeded:
        send_commands_to_printer_if_needed(printer.id)
    return render(request, 'printer_acted.html', {'printer': printer, 'action': 'cancel', 'succeeded': succeeded, 'user_credited': user_credited})

@login_required
def resume_printer(request, pk):
    printer = get_printer_or_404(pk, request)
    succeeded, user_credited = printer.resume_print(mute_alert=request.GET.get('mute_alert', False))
    if succeeded:
        send_commands_to_printer_if_needed(printer.id)
    return render(request, 'printer_acted.html', {'printer': printer, 'action': 'resume', 'succeeded': succeeded, 'user_credited': user_credited})


# User preferences

@login_required
def user_preferences(request):
    form = UserPreferencesForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, 'Your preferences have been updated successfully!')

    return render(request, 'user_preferences.html', dict(form=form, bot_name=bot_name))

### Prints and public time lapse ###

@login_required
def prints(request):
    prints = get_prints(request).filter(video_url__isnull=False).order_by('-id')
    if request.GET.get('deleted', False):
        prints = prints.all(force_visibility=True)
    page_obj = get_paginator(prints, request, 9)
    prediction_urls = [ dict(print_id=print.id, prediction_json_url=print.prediction_json_url) for print in page_obj.object_list]
    return render(request, 'print_list.html', dict(prints=page_obj.object_list, page_obj=page_obj, prediction_urls=prediction_urls))

@login_required
def delete_prints(request, pk):
    if request.method == 'POST':
        select_prints_ids = request.POST.getlist('selected_print_ids', [])
    else:
        select_prints_ids = [pk]

    get_prints(request).filter(id__in=select_prints_ids).delete()
    messages.success(request, '{} time-lapses deleted.'.format(len(select_prints_ids)))
    return redirect(reverse('prints'))

@login_required
def upload_print(request):
    if request.method == 'POST':
        _, file_extension = os.path.splitext(request.FILES['file'].name)
        video_path = f'{str(timezone.now().timestamp())}{file_extension}'
        save_file_obj(f'uploaded/{video_path}', request.FILES['file'], settings.PICS_CONTAINER)
        celery_app.send_task('app_ent.tasks.credit_dh_for_contribution', args=[request.user.id, 1, 'Credit | Upload "{}"'.format(request.FILES['file'].name[:100])])
        preprocess_timelapse.delay(request.user.id, video_path, request.FILES['file'].name)

        return JsonResponse(dict(status='Ok'))
    else:
        return render(request, 'upload_print.html')

def publictimelapse_list(request):
    timelapses_list = list(PublicTimelapse.objects.order_by('priority').values())
    page_obj = get_paginator(timelapses_list, request, 9)
    return render(request, 'publictimelapse_list.html', dict(timelapses=page_obj.object_list, page_obj=page_obj))


### Consent page #####

@login_required
def consent(request):
    if request.method == 'POST':
        user = request.user
        user.consented_at = timezone.now()
        user.save()
        return redirect('/printers/')
    else:
        return render(request, 'consent.html')

def webrtc(request):
    return render(request, 'webrtc.html')

# Was surprised to find there is no built-in way in django to serve uploaded files in both debug and production mode

def serve_jpg_file(request, file_path):
    full_path = os.path.join(settings.MEDIA_ROOT, file_path)
    if not os.path.exists(full_path):
        raise Http404("Requested file does not exist")
    with open(full_path, 'rb') as fh:
        return HttpResponse(fh, content_type='image/jpeg')
