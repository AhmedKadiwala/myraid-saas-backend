from rest_framework.pagination import PageNumberPagination


class LegacyPagination(PageNumberPagination):
    page_query_param = "page"
    page_size_query_param = "rows"
    max_page_size = 200
