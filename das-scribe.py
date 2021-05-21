from html.parser import HTMLParser
from io import StringIO
import argparse
import markdown
import os
import os.path
import shutil
import sys
import time


class Error(Exception):
    pass


class TemplateError(Error):
    def __init__(self, tmpl, msg):
        self.tmpl = tmpl
        self.msg = msg

    def __str__(self):
        return 'Error in template %r: %s' % (self.tmpl, self.msg)


def CreateParser():
    parser = argparse.ArgumentParser(description='Static content blogging')
    parser.add_argument('input', metavar='input_dir', type=str)
    parser.add_argument('output', metavar='output_dir', type=str)
    parser.add_argument('--template',
                        metavar='template',
                        type=str,
                        default='template.html')
    parser.add_argument('--index_template',
                        metavar='index_template',
                        type=str,
                        default='')
    parser.add_argument('--link_prefix', type=str, default='')
    parser.add_argument('-n'
                        '--dry_run',
                        metavar='dry_run',
                        type=bool,
                        default=False,
                        dest='dry_run')
    return parser


def _PathHasDotfiles(path):
    base, tail = os.path.split(path)
    while tail:
        if tail.startswith('.') and len(tail) > 1:
            return True
        base, tail = os.path.split(base)
    return False


FILETYPE_OTHER = 1
FILETYPE_MD = 2


class ItemFile(object):
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.ctime = None
        try:
            self.mtime = os.path.getmtime(path)
            self.ctime = os.path.getctime(path)
        except os.error:
            pass


class Item(object):
    def __init__(self, ft, src, dst):
        self.ft = ft
        self.src = ItemFile(src)
        self.dst = ItemFile(dst)

    @property
    def ctime(self):
        # This logic should definitely be fixed.
        return self.src.ctime or time.time()


class Plan(object):
    def __init__(self):
        self._items = []
        self._items_by_dir = {}

    def AddItem(self, ft, src, dst, post_dir=None):
        item = Item(ft, src, dst)
        self._items.append(item)

        dir_list = self._items_by_dir.get(post_dir, None)
        if not dir_list:
            dir_list = []
            self._items_by_dir[post_dir] = dir_list
        dir_list.append(item)

    def IterDirs(self):
        sorted_dirs = sorted(item for item in self._items_by_dir.items())
        for dir_name, items in sorted_dirs:
            mds = [item for item in items if item.ft == FILETYPE_MD]
            if any(mds):
                if len(mds) > 1:
                    sys.exit(
                        'Found more than 1 .md file in directory %r; aborting.'
                        % path)
                post_name = os.path.splitext(os.path.basename(
                    mds[0].src.path))[0]
                yield dir_name, post_name, items


class Template(object):
    def __init__(self, path):
        self._path = path
        try:
            with open(path, 'r') as open_file:
                self._contents = open_file.read()
        except StandardError as e:
            sys.exit('Error reading template file %r\n%s' % (path, e))
        self._Validate()

    def _Validate(self):
        if '{{content}}' not in self._contents:
            raise TemplateError(self._path, 'missing {{content}} var')

    def _ExtractTitle(self, md_str):
        class TitleExtractor(HTMLParser.HTMLParser):
            def __init__(self):
                HTMLParser.HTMLParser.__init__(self)
                self._reading = True
                self._text = []
                self._tag_stack = []

            def title(self):
                return ''.join(self._text)

            def handle_starttag(self, tag, attrs):
                if self._reading:
                    self._tag_stack.append(tag)

            def handle_endtag(self, tag):
                if self._reading:
                    self._tag_stack.pop()
                    if not self._tag_stack:
                        self._reading = False

            def handle_data(self, data):
                if self._reading:
                    self._text.append(data)

            def handle_entityref(self, name):
                if self._reading:
                    self._text.append('&%s;' % name)

        parser = TitleExtractor()
        parser.feed(md_str)
        return parser.title()

    def Fill(self, contents, next_post=None, prev_post=None):
        next_post = next_post or ''
        prev_post = prev_post or ''

        title = self._ExtractTitle(contents)

        tmpl = self._contents.replace('{{content}}', contents)
        tmpl = tmpl.replace('{{title}}', title)
        tmpl = tmpl.replace('{{newer_link}}', next_post)
        tmpl = tmpl.replace('{{older_link}}', prev_post)
        return title, tmpl


class Blog(object):
    def __init__(self, args):
        self._input_dir = os.path.abspath(args.input)
        self._output_dir = os.path.abspath(args.output)
        self._post_template = Template(args.template)
        self._index_template = Template(args.index_template or args.template)
        self._link_prefix = args.link_prefix

    def _WriteIndexFile(self, items):
        index_md_io = StringIO()
        index_md_io.write('Recent posts\n======\n')
        for (title, item), (path, post_name, _) in items:
            post_link = '%s/%s/%s.html' % (self._link_prefix, path, post_name)
            index_md_io.write('* %s - [%s](%s)\n' % (time.strftime(
                '%d %b %Y', time.gmtime(item.ctime)), title, post_link))
        index_html = markdown.markdown(index_md_io.getvalue(),
                                       extensions=['footnotes'])
        _, index_contents = self._index_template.Fill(index_html)
        index_path = os.path.join(self._output_dir, 'index.html')
        print('writing index to %s' % index_path)
        with open(index_path, 'w') as output_file:
            output_file.write(index_contents)

    def Generate(self, dry_run=False):
        plan = self._BuildPlan()
        print('From %s, output to %s' % (self._input_dir, self._output_dir))
        all_dirs = list(plan.IterDirs())
        posts = []
        for dir_index, (path, post_name, items) in enumerate(all_dirs):
            print('  PLAN: for dir %r, post %r' % (path, post_name))
            prev_post, next_post = ('%s/index.html' % self._link_prefix, ) * 2
            if dir_index:
                prev_post = '%s/%s/%s.html' % (self._link_prefix,
                                               all_dirs[dir_index - 1][0],
                                               all_dirs[dir_index - 1][1])
            if dir_index + 1 < len(all_dirs):
                next_post = '%s/%s/%s.html' % (self._link_prefix,
                                               all_dirs[dir_index + 1][0],
                                               all_dirs[dir_index + 1][1])
            if not os.path.exists(os.path.join(self._output_dir, path)):
                print('    creating %s' % path)
                if not dry_run:
                    os.makedirs(os.path.join(self._output_dir, path))
            for item in items:
                dst_contents = None
                if item.ft == FILETYPE_MD:
                    print()
                    print(item.ft)
                    print(item)
                    print()
                    md_io = StringIO()
                    markdown.markdownFromFile(input=item.src.path,
                                              output=md_io)
                    title, dst_contents = self._post_template.Fill(
                        md_io.getvalue(),
                        next_post=next_post,
                        prev_post=prev_post)
                    posts.append((title, item))
                    print('    expanded template %s: %r' %
                          (item.src.path, title))
                print('    handling %r --> %r' %
                      (item.src.path, item.dst.path))
                if not dry_run:
                    if dst_contents:
                        with open(item.dst.path, 'w') as output_file:
                            output_file.write(dst_contents)
                    else:
                        shutil.copy(item.src.path, item.dst.path)

        if not dry_run:
            self._WriteIndexFile(reversed(zip(posts, all_dirs)))

    def _BuildPlan(self):
        plan = Plan()
        for path, _, filenames in os.walk(self._input_dir):
            print('in path %s' % path)
            common_prefix = os.path.commonprefix(
                [os.path.abspath(path), self._output_dir])
            if common_prefix == self._output_dir:
                print('  skipping self')
                continue
            if _PathHasDotfiles(path):
                print('  skipping dotfile')
                continue
            path = path[len(self._input_dir) + 1:]
            target_path = os.path.join(self._output_dir, path)
            for filename in filenames:
                print('  considering %s' % filename)
                if filename.startswith('.'):
                    print('    skipping dotfile')
                    continue
                ext = os.path.splitext(filename)[1]
                print('---')
                print(os.path.splitext(filename))
                print('+++')
                input_name = os.path.join(self._input_dir, path, filename)
                output_name = os.path.join(target_path, filename)
                ft = FILETYPE_OTHER
                if ext == '.md':
                    ft = FILETYPE_MD
                    output_name = '%s.html' % output_name[:-3]
                print('>>> ', FILETYPE_OTHER, ft)
                plan.AddItem(ft, input_name, output_name, post_dir=path)
        return plan


def main(argv):
    parser = CreateParser()
    args = parser.parse_args(argv[1:])

    blog = Blog(args)
    blog.Generate(dry_run=args.dry_run)


if __name__ == '__main__':
    main(sys.argv)
