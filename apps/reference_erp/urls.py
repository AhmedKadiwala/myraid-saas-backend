from django.urls import path
from .views import WorkspaceView, CommandsView, RecordsView

urlpatterns = [
    path("workspace/", WorkspaceView.as_view()),
    path("commands/", CommandsView.as_view()),
    path("records/<str:collection>/", RecordsView.as_view()),
    path("records/<str:collection>/<str:code>/", RecordsView.as_view()),
]
