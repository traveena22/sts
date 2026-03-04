from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.db import transaction
import random

# Model Imports
from core.models import Section, Subject, Classroom
from scheduler.models import TimeSlot
from faculty.models import Faculty, FacultyAvailability
from notifications.models import Notification
from .models import TimetableEntry

# PDF Generation
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet


# ----------------------------------------------------------
# GENERATE TIMETABLE
# ----------------------------------------------------------

@login_required
def generate_timetable(request):
    """
    Heuristic-based timetable generation engine.
    Ensures subjects are spread across the week, respects faculty availability,
    and assigns specific teachers and rooms to every slot.
    """
    if request.user.role != 'admin':
        return redirect('user_dashboard')

    if request.method == 'POST':
        with transaction.atomic():
            # 1. Clear existing schedule
            TimetableEntry.objects.all().delete()

            sections = Section.objects.all()
            # Convert QuerySets to lists for faster iteration
            classrooms = list(Classroom.objects.filter(is_available=True))
            faculty_members = list(Faculty.objects.all())
            days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

            for section in sections:
                # Get subjects relevant to this specific section
                subjects = list(Subject.objects.filter(
                    semester=section.semester, 
                    department=section.department
                ))
                
                # Track how many times each subject has been scheduled this week
                subject_usage = {s.id: 0 for s in subjects}

                for day in days:
                    previous_subject_id = None
                    
                    # Fetch only teaching slots (exclude breaks) for the current day
                    slots = TimeSlot.objects.filter(day=day, is_break=False).order_by('start_time')

                    for slot in slots:
                        assigned = False
                        
                        # MODULE 1: Heuristic Subject Selection
                        for subject in subjects:
                            if subject_usage[subject.id] >= subject.classes_per_week:
                                continue
                            if subject.id == previous_subject_id:
                                continue

                            # MODULE 2: Room Allocation
                            suitable_room = None
                            for room in classrooms:
                                if subject.subject_type == 'lab' or subject.code.endswith('L'):
                                    if room.room_type != 'lab' and "Lab" not in room.name:
                                        continue
                                
                                if room.capacity < section.student_count:
                                    continue

                                if not TimetableEntry.objects.filter(
                                    classroom=room, day=day, timeslot=slot
                                ).exists():
                                    suitable_room = room
                                    break
                            
                            if not suitable_room:
                                continue

                            # MODULE 3: Smart Faculty Allocation (Balanced & Rotational)
                            suitable_teacher = None

                            # Step 1: Create a temporary list of all faculty members to shuffle
                            # This prevents the algorithm from always picking the same teacher first
                            shuffled_faculty = list(faculty_members) 
                            import random
                            random.shuffle(shuffled_faculty) 

                            for f in shuffled_faculty:
                                # 1. Department Match: Faculty must belong to the subject's department
                                if f.department != subject.department:
                                    continue
                                
                                # 2. Availability Matrix Check:
                                # We use day.lower() to match 'monday' storage and check for is_available=False
                                is_busy = FacultyAvailability.objects.filter(
                                    faculty=f, 
                                    day=day.lower(), 
                                    start_time=slot.start_time,
                                    is_available=False
                                ).exists()

                                if is_busy:
                                    continue # Skip this teacher if they marked this specific slot as "Busy"

                                # 3. Workload Check: Enforce limits set in Faculty Profile
                                # Count how many classes this teacher already has in the current generation session
                                current_daily_load = TimetableEntry.objects.filter(faculty=f, day=day).count()
                                current_weekly_load = TimetableEntry.objects.filter(faculty=f).count()

                                if current_daily_load >= (f.max_classes_per_day or 5):
                                    continue # Skip if daily limit reached
                                    
                                if current_weekly_load >= (f.max_classes_per_week or 20):
                                    continue # Skip if weekly limit reached

                                # 4. Collision Check: Ensure teacher isn't teaching another section at this time
                                if not TimetableEntry.objects.filter(faculty=f, day=day, timeslot=slot).exists():
                                    suitable_teacher = f
                                    break # Teacher is free, available, and within limits!
                            # MODULE 4: Creation of Final Entry
                            if suitable_room and suitable_teacher:
                                TimetableEntry.objects.create(
                                    subject=subject,
                                    section=section,
                                    classroom=suitable_room,
                                    faculty=suitable_teacher,
                                    timeslot=slot,
                                    day=day
                                )
                                subject_usage[subject.id] += 1
                                previous_subject_id = subject.id
                                assigned = True
                                break 

            # 5. Final Validation Check
            total_required = TimeSlot.objects.filter(is_break=False).count() * sections.count()
            actual_created = TimetableEntry.objects.count()

            if actual_created >= total_required:
                messages.success(request, "Full-week timetable generated successfully.")
            else:
                messages.warning(request, f"Timetable generated. {actual_created}/{total_required} slots filled.")

        return redirect('timetable_view')

    return render(request, 'timetable/generate_confirm.html', {
        'total_sections': Section.objects.count()
    })

# ----------------------------------------------------------
# GRID VIEW (UPDATED: Shows all sections in dropdown)
# ----------------------------------------------------------

@login_required
def timetable_grid_view(request):
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

    unique_timeslots = (
        TimeSlot.objects
        .order_by('start_time')
        .values('start_time', 'end_time', 'is_break', 'break_name')
        .distinct()
    )

    section_id = request.GET.get('section')

    if section_id:
        # Optimized with select_related to get subject, faculty, and classroom names
        entries = TimetableEntry.objects.filter(section_id=section_id).select_related('subject', 'faculty__user', 'classroom')
    else:
        entries = TimetableEntry.objects.all().select_related('subject', 'faculty__user', 'classroom')

    grid = {day: {} for day in days}

    for entry in entries:
        key = entry.timeslot.start_time.strftime('%H:%M')
        grid[entry.day][key] = entry

    return render(request, 'timetable/timetable_view.html', {
        'days': days,
        'timeslots': unique_timeslots,
        'grid': grid,
        # UPDATED: Pull all sections so IT and others show up even if empty
        'sections': Section.objects.all().select_related('department'),
        'selected_section': section_id
    })


# ----------------------------------------------------------
# EXPORT PDF (UPDATED: Includes Teacher Name)
# ----------------------------------------------------------

def export_timetable_pdf(request, section_id):
    section = get_object_or_404(Section.objects.select_related('department'), id=section_id)

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Timetable_{section.name}.pdf"'

    doc = SimpleDocTemplate(response, pagesize=landscape(A4))
    elements = []
    styles = getSampleStyleSheet()

    elements.append(
        Paragraph(
            f"Official Timetable - Section {section.name} ({section.department.code})",
            styles['Title']
        )
    )

    data = [['Time', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']]

    times = (
        TimeSlot.objects
        .order_by('start_time')
        .values('start_time', 'end_time', 'is_break', 'break_name')
        .distinct()
    )

    for t in times:
        time_str = t['start_time'].strftime('%H:%M')
        row = [time_str]

        for d in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']:
            if t['is_break']:
                row.append(t['break_name'] or "BREAK")
            else:
                entry = TimetableEntry.objects.filter(
                    section=section,
                    day=d,
                    timeslot__start_time=t['start_time']
                ).select_related('subject', 'faculty__user', 'classroom').first()

                if entry:
                    # UPDATED: Added Teacher email split to the PDF row
                    teacher_name = entry.faculty.user.email.split('@')[0] if entry.faculty else "TBD"
                    row.append(f"{entry.subject.code}\n{entry.classroom.name}\n({teacher_name})")
                else:
                    row.append("-")

        data.append(row)

    table = Table(data, hAlign='CENTER')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E3A8A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
    ]))

    elements.append(table)
    doc.build(elements)

    return response


# ----------------------------------------------------------
# PUBLISH TIMETABLE
# ----------------------------------------------------------

@login_required
def publish_timetable(request):
    if request.method == 'POST':
        entries = TimetableEntry.objects.all()
        faculty_users = (
            Faculty.objects
            .filter(timetableentry__in=entries)
            .values_list('user', flat=True)
            .distinct()
        )

        for user_id in faculty_users:
            Notification.objects.create(
                user_id=user_id,
                title="Timetable Published",
                message="The final academic schedule is now live."
            )

        messages.success(request, "Timetable published successfully.")

    return redirect('timetable_view')


# ----------------------------------------------------------
# PUBLIC VIEW (UPDATED: Matches Grid logic)
# ----------------------------------------------------------

def public_timetable_view(request):
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    unique_timeslots = TimeSlot.objects.values('start_time', 'end_time', 'is_break', 'break_name').distinct().order_by('start_time')
    
    section_id = request.GET.get('section')
    # UPDATED: Pull all sections for the public dropdown
    sections = Section.objects.all().select_related('department')
    
    grid = {day: {} for day in days}
    if section_id:
        entries_qs = TimetableEntry.objects.filter(section_id=section_id).select_related('subject', 'faculty__user', 'classroom')
        for entry in entries_qs:
            time_key = entry.timeslot.start_time.strftime('%H:%M')
            grid[entry.day][time_key] = entry

    return render(request, 'timetable/public_view.html', {
        'days': days,
        'timeslots': unique_timeslots,
        'grid': grid,
        'sections': sections,
        'selected_section': section_id
    })