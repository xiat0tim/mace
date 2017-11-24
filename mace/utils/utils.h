//
// Copyright (c) 2017 XiaoMi All rights reserved.
//

#ifndef MACE_UTILS_UTILS_H_
#define MACE_UTILS_UTILS_H_

#include <sys/time.h>

namespace mace {
template <typename Integer>
Integer RoundUp(Integer i, Integer factor) {
  return (i + factor - 1) / factor * factor;
}

template <typename Integer, uint32_t factor>
Integer RoundUpDiv(Integer i) {
  return (i + factor - 1) / factor;
}

// Partial specialization of function templates is not allowed
template <typename Integer>
Integer RoundUpDiv4(Integer i) {
  return (i + 3) >> 2;
}

template <typename Integer>
Integer RoundUpDiv8(Integer i) {
  return (i + 7) >> 3;
}

template <typename Integer>
Integer CeilQuotient(Integer a, Integer b) {
  return (a + b - 1) / b;
}

inline int64_t NowInMicroSec() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return static_cast<int64_t>(tv.tv_sec * 1000000 + tv.tv_usec);
}

}  //  namespace mace
#endif  //  MACE_UTILS_UTILS_H_