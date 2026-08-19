from rest_framework import serializers
from rest_framework.generics import GenericAPIView


class EmptySerializer(serializers.Serializer):
    """Schema fallback for legacy envelope endpoints with hand-built responses."""


class APIView(GenericAPIView):
    serializer_class = EmptySerializer
