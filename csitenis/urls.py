"""
URL configuration for csitenis project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from reservations.views import (
    AdminTokenObtainPairView,
    AdminTokenRefreshView,
    AuthMeAPIView,
    AuthUserDetailAPIView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('reservations.urls')),
    path('api/token/', AdminTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', AdminTokenRefreshView.as_view(), name='token_refresh'),
    path('api/auth/me/', AuthMeAPIView.as_view(), name='auth_me'),
    path('api/auth/users/<int:user_id>/', AuthUserDetailAPIView.as_view(), name='auth_user_detail'),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]
