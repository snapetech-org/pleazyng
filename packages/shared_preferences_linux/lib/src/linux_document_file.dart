import 'dart:ffi';
import 'dart:io';
import 'dart:math' as math;

import 'package:ffi/ffi.dart';

final _libc = DynamicLibrary.process();
final _open = _libc.lookupFunction<Int Function(Pointer<Utf8>, Int, VarArgs<(UnsignedInt,)>),
    int Function(Pointer<Utf8>, int, int)>('open');
final _flock = _libc.lookupFunction<Int Function(Int, Int), int Function(int, int)>('flock');
final _write =
    _libc.lookupFunction<IntPtr Function(Int, Pointer<Uint8>, Size), int Function(int, Pointer<Uint8>, int)>('write');
final _fsync = _libc.lookupFunction<Int Function(Int), int Function(int)>('fsync');
final _fchmod = _libc.lookupFunction<Int Function(Int, UnsignedInt), int Function(int, int)>('fchmod');
final _close = _libc.lookupFunction<Int Function(Int), int Function(int)>('close');

// Linux open(2) flags common to the supported x64 and arm64 targets.
const _readWrite = 2, _create = 0x40, _exclusive = 0x80, _closeOnExec = 0x80000, _noFollow = 0x20000;
const _ownerReadWrite = 0x180;

int _openPrivate(String path, int flags) {
  if (path.contains('\x00')) throw const FileSystemException('Invalid preferences path');
  final name = path.toNativeUtf8();
  try {
    final fd = _open(name, flags | _closeOnExec | _noFollow, _ownerReadWrite);
    if (fd < 0) throw FileSystemException('Could not open preferences file', path);
    return fd;
  } finally {
    malloc.free(name);
  }
}

// flock belongs to the open handle. Separate isolates in one process contend
// for it too, unlike the process-scoped POSIX record locks used by dart:io.
T withLinuxDocumentLock<T>(String path, bool writing, T Function() run) {
  final fd = _openPrivate(path, writing ? _readWrite | _create : 0);
  try {
    const shared = 1, exclusive = 2;
    if (_flock(fd, writing ? exclusive : shared) != 0) {
      throw FileSystemException('Could not lock preferences file', path);
    }
    return run();
  } finally {
    _close(fd);
  }
}

/// Write and flush a new owner-only file using the descriptor that created it.
void writePrivateLinuxPreferences(String path, List<int> bytes) {
  final fd = _openPrivate(path, _readWrite | _create | _exclusive);
  try {
    if (bytes.isNotEmpty) {
      final capacity = math.min(bytes.length, 65536);
      final buffer = malloc<Uint8>(capacity);
      try {
        final chunk = buffer.asTypedList(capacity);
        for (var offset = 0; offset < bytes.length;) {
          final length = math.min(capacity, bytes.length - offset);
          chunk.setRange(0, length, bytes, offset);
          var written = 0;
          while (written < length) {
            final count = _write(fd, buffer + written, length - written);
            if (count <= 0) throw FileSystemException('Could not write preferences file', path);
            written += count;
          }
          offset += length;
        }
      } finally {
        malloc.free(buffer);
      }
    }
    if (_fsync(fd) != 0) throw FileSystemException('Could not flush preferences file', path);
  } finally {
    _close(fd);
  }
}

/// Protect a legacy document before moving it into the recovery archive.
void restrictLinuxPreferences(String path) {
  final fd = _openPrivate(path, 0);
  try {
    if (_fchmod(fd, _ownerReadWrite) != 0) throw FileSystemException('Could not protect preferences file', path);
  } finally {
    _close(fd);
  }
}
