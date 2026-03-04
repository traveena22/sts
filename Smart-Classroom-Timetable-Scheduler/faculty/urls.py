from django.urls import path
from . import views

urlpatterns = [
    path('list/', views.FacultyListView.as_view(), name='faculty_list'),
    path('add/', views.FacultyCreateView.as_view(), name='faculty_add'),
    # ADD THESE TWO LINES:
    path('edit/<int:pk>/', views.FacultyUpdateView.as_view(), name='faculty_edit'),
    path('delete/<int:pk>/', views.faculty_delete, name='faculty_delete'),
    
    path('dashboard/', views.faculty_dashboard, name='faculty_dashboard'),
    path('availability/', views.availability_matrix, name='faculty_availability'),
]