def to_jsonable(value):
    """Приводит значение к типам, которые json стандартной библиотеки умеет писать.

    Сравнение чисел numpy даёт numpy.bool_, а json в Python 3.5 такой объект
    не сериализует и падает с «True is not JSON serializable».
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]

    as_list = getattr(value, "tolist", None)
    if callable(as_list):
        try:
            return to_jsonable(as_list())
        except Exception:
            pass
    return str(value)
