part of 'webdav_source.dart';

typedef _DavState = ({Uri uri, Map<String, dynamic> data, String? etag, bool exists});

extension _WebDavState on WebDavSourceAdapter {
  // RFC 9110 section 8.8.3. An update needs a single strong validator.
  static final _strongEtag = RegExp(r'^"[\x21\x23-\x7e\x80-\xff]*"$');
  static const maxBytes = 2 * 1024 * 1024;

  Future<_DavState> _readState(Uri uri) async {
    final response = await transport.send('GET', uri, headers: await headers());
    if (response.statusCode < 200 || response.statusCode >= 300) {
      await response.stream.listen(null).cancel();
      if (response.statusCode == 404) return (uri: uri, data: <String, dynamic>{}, etag: null, exists: false);
      SourceTransport.check(response.statusCode);
    }
    final bytes = <int>[];
    await for (final chunk in response.stream.timeout(const Duration(seconds: 30))) {
      if (bytes.length + chunk.length > maxBytes) throw const SourceException(SourceFailure.invalidData);
      bytes.addAll(chunk);
    }
    final data = sourceMap(jsonDecode(utf8.decode(bytes)));
    if (data['version'] != 1) throw const SourceException(SourceFailure.unsupported);
    return (uri: uri, data: data, etag: response.headers['etag'], exists: true);
  }

  Future<void> _writeState(_DavState reviewed, Map<String, dynamic> data) async {
    final etag = reviewed.etag;
    if (reviewed.exists && (etag == null || !_strongEtag.hasMatch(etag))) {
      throw const SourceException(SourceFailure.unsupported);
    }
    final body = utf8.encode(jsonEncode({'version': 1, ...data}));
    if (body.length > maxBytes) throw const SourceException(SourceFailure.quotaExceeded);
    final response = await transport.send(
      'PUT',
      reviewed.uri,
      headers: {
        ...await headers(),
        'Content-Type': 'application/json',
        if (reviewed.exists) 'If-Match': etag! else 'If-None-Match': '*',
      },
      body: body,
    );
    await response.stream.listen(null).cancel();
    SourceTransport.check(response.statusCode);
  }
}
