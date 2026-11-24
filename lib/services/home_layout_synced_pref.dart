part of 'home_layout_sync_service.dart';

/// One synced pref with its JSON round trip.
class _SyncedPref<T> {
  _SyncedPref._(this.pref, this._toJson, this._fromJson, this._defaultJson);

  // Non-generic return type on purpose: a `_SyncedPref<V>` return lets the
  // list's `_SyncedPref<dynamic>` context infer V = dynamic, which erases the
  // pref's real type and fails the runtime check on `JsonPref.encode`.
  static _SyncedPref<dynamic> json<V>(JsonPref<V> pref) => _SyncedPref<V>._(
    pref,
    (v) => jsonDecode(pref.encode(v)),
    (raw) => pref.decode(raw),
    jsonDecode(pref.encode(pref.defaultValue)),
  );

  static _SyncedPref<dynamic> boolean(BoolPref pref) =>
      _SyncedPref<bool>._(pref, (v) => v, (raw) => raw as bool? ?? pref.defaultValue, pref.defaultValue);

  final Pref<T> pref;
  final Object? Function(T) _toJson;
  final T Function(Object?) _fromJson;
  final Object? _defaultJson;

  Object? encode(BaseSharedPreferencesService settings) => _toJson(settings.read(pref));

  Object? normalize(Object? raw) => _toJson(_fromJson(raw));

  bool isDefault(BaseSharedPreferencesService settings) => jsonEncode(encode(settings)) == jsonEncode(_defaultJson);

  Future<void> apply(BaseSharedPreferencesService settings, Object? raw) => settings.write(pref, _fromJson(raw));
}
