part of 'image_cache_service.dart';

/// Shared cache manager for media-server image artwork. Used for both Plex and
/// Jellyfin artwork (the class name predates Jellyfin support — it's
/// backend-neutral).
///
/// Artwork rails are the widest fan-out in the app, so the wrapper below keeps
/// it bounded — weak TV devices must not decode a whole rail at once — while
/// the shared platform client supplies the connection pool it fans out over.
class PlexImageCacheManager extends ce_cache.DefaultCacheManager {
  static final PlexImageCacheManager instance = PlexImageCacheManager._();

  PlexImageCacheManager._()
    : this.forTesting(
        httpClientFactory: () => _SharedHttpClient(_artworkHttpClient.inner, _artworkRequestLimiter),
        cacheDirectoryProvider: getPlezyAppCacheDirectory,
      );

  @visibleForTesting
  PlexImageCacheManager.forTesting({
    required http.Client Function() httpClientFactory,
    required ce_cache.CacheDirectoryProvider cacheDirectoryProvider,
  }) : super(
         stalePeriod: const Duration(days: 14),
         maxNrOfCacheObjects: 3000,
         httpClientFactory: httpClientFactory,
         cacheDirectoryProvider: cacheDirectoryProvider,
         httpInterceptors: const [_ArtworkCredentialInterceptor()],
       );

  // The cache library stores its URL and key on disk. Give it opaque values
  // and restore the actual URL only when its HTTP interceptor requests it.
  @override
  Stream<FileResponse> getFileStream(
    String url, {
    String? key,
    Map<String, String>? headers,
    bool withProgress = false,
  }) {
    final cacheUrl = _artworkId(url);
    // User-Agent is the only extra header the poster widgets set. Other
    // request headers can select a private representation of the same URL.
    final variants = SplayTreeMap<String, String>();
    headers?.forEach((name, value) {
      if (name.toLowerCase() != 'user-agent') variants[name.toLowerCase()] = value;
    });
    final cacheKey = variants.isEmpty ? _artworkId(key ?? url) : _artworkId(jsonEncode([key ?? url, variants]));
    return super.getFileStream(
      cacheUrl,
      key: cacheKey,
      headers: {...?headers, _credentialedUrlHeader: url},
      withProgress: withProgress,
    );
  }

  @override
  Future<FileInfo?> getFileFromCache(String key, {bool ignoreMemCache = false}) async {
    final hit = await super.getFileFromCache(_artworkId(key), ignoreMemCache: ignoreMemCache);
    if (hit == null || _storedArtworkId.hasMatch(hit.originalUrl)) return hit;
    // Older poster entries already have an account-qualified hash as key.
    // Keep the image available offline without returning its old display URL.
    return FileInfo(hit.file, hit.source, hit.validTill, _artworkId(hit.originalUrl), statusCode: hit.statusCode);
  }

  @override
  Future<void> removeFile(String key) => super.removeFile(_artworkId(key));

  @override
  Future<File> putFile(
    String url,
    List<int> fileBytes, {
    String? key,
    String? eTag,
    Duration maxAge = const Duration(days: 30),
    String fileExtension = 'file',
  }) {
    return super.putFile(
      _artworkId(url),
      fileBytes,
      key: key == null ? null : _artworkId(key),
      eTag: eTag,
      maxAge: maxAge,
      fileExtension: fileExtension,
    );
  }

  @override
  Stream<FileResponse> getImageFile(
    String url, {
    String? key,
    Map<String, String>? headers,
    bool withProgress = false,
    int? maxHeight,
    int? maxWidth,
  }) {
    // Plezy already requests server-sized artwork URLs. Avoid CE's disk-resize
    // path, which decodes downloaded images before writing resized PNG copies.
    return getFileStream(url, key: key, headers: headers, withProgress: withProgress);
  }
}

// The library calls the public lookup methods with the already-translated
// key during a download. Keep that round trip idempotent.
final _storedArtworkId = RegExp(r'^(?:pleazyng-art-v2:[0-9a-f]{64}|plex_optimized_[0-9a-f]{40})$');

String _artworkId(String value) =>
    _storedArtworkId.hasMatch(value) ? value : 'pleazyng-art-v2:${sha256.convert(utf8.encode(value))}';

/// Request header carrying the credentialed URL from
/// [PlexImageCacheManager.getFileStream] to [_ArtworkCredentialInterceptor].
/// Never sent: the interceptor removes it.
const _credentialedUrlHeader = 'x-plezy-credentialed-url';

class _ArtworkCredentialInterceptor extends HttpInterceptor {
  const _ArtworkCredentialInterceptor();

  @override
  void onRequest(HttpRequestData request, HttpRequestHandler handler) {
    final credentialedUrl = request.headers.remove(_credentialedUrlHeader);
    if (credentialedUrl != null) request.url = credentialedUrl;
    handler.next(request);
  }
}
